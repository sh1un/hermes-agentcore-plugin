import asyncio
from contextvars import ContextVar
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agentcore_core.chat_retry import ChatRetry
from agentcore_core.connections import SlackIdentity


class ChatRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.alice = SlackIdentity("TTEST", "UALICE")
        self.bob = SlackIdentity("TTEST", "UBOB")
        self.runtime, self.adapter = Mock(), Mock()
        self.adapter._early_reject_unauthorized.return_value = False
        self.adapter.build_source.side_effect = lambda **kwargs: types.SimpleNamespace(**kwargs)
        self.adapter.handle_message = AsyncMock()
        self.client = Mock(chat_postMessage=AsyncMock(return_value={"ts": "dm-prompt"}),
                           chat_postEphemeral=AsyncMock(return_value={"message_ts": "prompt"}))
        self.chat = ChatRetry(self.runtime, self.adapter)
        self.vars = {"HERMES_SESSION_" + k: ContextVar(k) for k in ("CHAT_ID", "MESSAGE_ID", "THREAD_ID")}
        self.enterContext(patch.dict(sys.modules, {
            "gateway": types.SimpleNamespace(session_context=types.SimpleNamespace(_VAR_MAP=self.vars)),
            "gateway.platforms.base": types.SimpleNamespace(MessageEvent=types.SimpleNamespace)}))
        self.runtime.portal_url = "https://portal.example"
        self.runtime.status.return_value = {"gateway": "connected"}

    def turn(self, issue="TEST-1", ts="11", thread="10", channel="CTEST"):
        self.chat.observe(self.alice, {"event": {"type": "message", "user": self.alice.member, "ts": ts, "thread_ts": thread,
            "channel": channel, "text": "Read " + issue}}, self.client)
        for k, value in (("CHAT_ID", channel), ("MESSAGE_ID", ts), ("THREAD_ID", thread)):
            self.vars["HERMES_SESSION_" + k].set(value)

    async def drain(self):
        await asyncio.sleep(0)
        if self.chat.tasks:
            await asyncio.gather(*list(self.chat.tasks))

    async def run_tool(self):
        result = self.chat.execute(self.alice, "getAccessibleAtlassianResources", {})
        await self.drain()
        return result

    async def click(self, token, who=None, channel="CTEST", msg="prompt"):
        return await self.chat.click(who or self.alice, {"actions": [{"value": token, "action_ts": "31"}],
            "channel": {"id": channel}, "container": {"message_ts": msg}}, self.client)

    async def test_login_then_consent_then_resume_issue_after_site_discovery(self):
        self.turn()
        self.runtime.execute.return_value = {"error": "connection_required"}
        self.runtime.connect.return_value = "https://login.example/private"
        result = await self.run_tool()
        self.assertNotIn("https:", json.dumps(result))
        self.assertEqual(self.client.chat_postEphemeral.call_args.kwargs["user"], self.alice.member)
        self.runtime.execute.return_value = {"error": "tool_authorization_required"}
        await self.run_tool()
        self.assertEqual(self.client.chat_postEphemeral.call_args.kwargs["blocks"][1]["elements"][0]["url"],
                         self.runtime.portal_url)
        token, = self.chat.tickets
        await self.click(token)
        await self.drain()
        event = self.adapter.handle_message.call_args.args[0]
        self.assertIn("TEST-1", event.text)
        self.assertEqual(event.source.thread_id, "10")
        self.assertEqual(event.source.user_id, self.alice.member)

    async def test_reverse_order_double_click_and_wrong_user_channel_message(self):
        self.runtime.execute.return_value = {"error": "tool_authorization_required"}
        self.turn()
        await self.run_tool()
        self.turn("TEST-2", "22", "20")
        await self.run_tool()
        first, second = self.chat.tickets
        for kwargs in ({"who": self.bob}, {"channel": "COTHER"}, {"msg": "other"}):
            self.assertEqual((await self.click(second, **kwargs)).status, 403)
        await self.click(second)
        await self.click(second)
        await self.click(first)
        await self.drain()
        events = [c.args[0] for c in self.adapter.handle_message.call_args_list]
        self.assertEqual([(e.source.thread_id, e.text.split()[-4]) for e in events],
                         [("20", "TEST-2"), ("10", "TEST-1")])

    async def test_cache_success_and_ordinary_errors(self):
        self.turn()
        for error in ("downstream_error", "invalid_arguments"):
            self.runtime.execute.return_value = {"error": error}
            self.assertEqual(await self.run_tool(), {"error": error})
        self.runtime.execute.side_effect = [{"error": "connection_required"}, {"result": "ok"}]
        self.runtime.connect.return_value = None
        self.assertEqual(await self.run_tool(), {"result": "ok"})
        self.runtime.connect.assert_called_once_with(self.alice, "gateway")
        self.client.chat_postEphemeral.assert_not_awaited()

    async def test_dm_service_failure_expiry_and_missing_context(self):
        self.turn(channel="DTEST", thread="")
        self.runtime.execute.return_value = {"error": "service_unavailable"}
        await self.run_tool()
        payload = self.client.chat_postMessage.call_args.kwargs
        self.assertNotIn("thread_ts", payload)
        self.assertEqual(len(payload["blocks"][1]["elements"]), 1)
        token, = self.chat.tickets
        with patch("agentcore_core.chat_retry.time.monotonic", return_value=float("inf")):
            await self.click(token, channel="DTEST", msg="dm-prompt")
        await self.drain()
        self.adapter.handle_message.assert_not_awaited()
        self.vars["HERMES_SESSION_MESSAGE_ID"].set("unrelated")
        self.assertEqual(await self.run_tool(), {"error": "service_unavailable"})
