"""Native Slack reauthentication stays scoped to its authenticated requester."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from slack_bolt.request.async_request import AsyncBoltRequest
from slack_bolt.response import BoltResponse

from agentcore_core import host, vault_gateway
from agentcore_core.connections import SlackIdentity
from agentcore_core.slack_ui import NativeConnections
from test_vault_gateway import settings


class ReauthenticationUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        gateway = types.ModuleType("gateway")
        gateway.session_context = types.SimpleNamespace(_VAR_MAP={key: object() for key in (
            "HERMES_SESSION_PLATFORM", "HERMES_SESSION_SCOPE_ID", "HERMES_SESSION_USER_ID", "HERMES_CRON_SESSION")})
        self.enterContext(patch.dict(sys.modules, {"gateway": gateway}))
        self.sdk, sts, session, http = Mock(), Mock(), Mock(), Mock()
        sts.assume_role.return_value = {"Credentials": {"AccessKeyId": "test", "SecretAccessKey": "test", "SessionToken": "test"}}
        session.client.return_value = self.sdk
        session.get_credentials.return_value.get_frozen_credentials.return_value = types.SimpleNamespace(
            access_key="test", secret_key="test", token="test")
        self.enterContext(patch("boto3.client", return_value=sts))
        self.enterContext(patch("boto3.Session", return_value=session))
        self.enterContext(patch("httpx.Client", return_value=http))
        self.enterContext(patch("agentcore_core.transport.call_mcp", return_value={"content": []}))
        cleanup = self.enterContext(patch.object(vault_gateway.atexit, "register"))
        self.sdk.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "private-workload"}
        self.sdk.get_resource_oauth2_token.return_value = {"accessToken": "private-existing"}
        http.post.return_value.status_code = 201
        raw = settings()
        raw["data_dir"] = directory.name
        self.ctx = Mock()
        self.runtime = vault_gateway.enable(self.ctx, raw)
        self.addCleanup(cleanup.call_args.args[0])
        app = Mock()
        self.ctx.register_platform_handler.call_args.args[1](app, None)
        self.ui = app.use.call_args.args[0]
        self.client = Mock(views_publish=AsyncMock())
        self.alice, self.bob = SlackIdentity("TTEST", "UALICE"), SlackIdentity("TTEST", "UBOB")
        self.runtime.connect(self.alice, "gateway")
        self.runtime.connect(self.bob, "gateway")
        self.sdk.get_resource_oauth2_token.reset_mock()
        self.uri = "urn:ietf:params:oauth:request_uri:forced-session"
        self.url = "https://bedrock-agentcore.ap-southeast-1.amazonaws.com/identities/oauth2/authorize?request_uri=" + self.uri
        self.next_listener = AsyncMock(side_effect=AssertionError("Auth action reached the model"))

    async def dispatch(self, action, *, user="UALICE", team="TTEST", trusted=True, ui=None):
        ui = ui or self.ui
        request = AsyncBoltRequest(body={"type": "block_actions", "team": {"id": team},
            "user": {"id": user}, "actions": [{"action_id": action, "value": "gateway"}]}, mode="socket_mode")
        request.context.update(team_id="TTEST", authorize_result=True if trusted else None, client=self.client)
        result = await ui.async_process(req=request, resp=BoltResponse(status=200), next=self.next_listener)
        if ui.tasks:
            await asyncio.gather(*list(ui.tasks))
        return result

    async def test_button_starts_one_forced_flow_then_refresh_and_tools_are_normal(self):
        await self.ui.home(self.client, self.alice)
        blocks = self.client.views_publish.call_args.kwargs["view"]["blocks"]
        button = next(element for block in blocks for element in block.get("elements", [])
                      if element.get("action_id") == "hac:reauthenticate")
        self.assertEqual(button["text"]["text"], "Sign in again")
        self.assertIn("confirm", button)
        self.sdk.get_resource_oauth2_token.return_value = {"authorizationUrl": self.url, "sessionUri": self.uri}
        self.assertEqual((await self.dispatch("hac:reauthenticate")).status, 200)
        self.assertIs(self.sdk.get_resource_oauth2_token.call_args.kwargs["forceAuthentication"], True)
        self.assertEqual(self.sdk.get_workload_access_token_for_user_id.call_args.kwargs["userId"], self.alice.subject)
        self.assertEqual(self.runtime.status(self.alice), {"gateway": "connecting"})
        self.assertEqual(self.runtime.status(self.bob), {"gateway": "connected"})
        self.assertIn(self.url, json.dumps(self.client.views_publish.call_args.kwargs))
        self.sdk.get_resource_oauth2_token.return_value = {"sessionStatus": "IN_PROGRESS"}
        await self.dispatch("hac:connect")
        polling = self.sdk.get_resource_oauth2_token.call_args.kwargs
        self.assertEqual(polling["sessionUri"], self.uri)
        self.assertNotIn("forceAuthentication", polling)
        self.sdk.get_resource_oauth2_token.return_value = {"accessToken": "private-new"}
        await self.dispatch("hac:connect")
        self.assertEqual(self.runtime.status(self.alice), {"gateway": "connected"})
        self.assertIn("result", self.runtime.execute(self.alice, "gateway", "getAccessibleAtlassianResources", {}))
        self.assertNotIn("forceAuthentication", self.sdk.get_resource_oauth2_token.call_args.kwargs)
        self.assertEqual(sum(c.kwargs.get("forceAuthentication", False) for c in self.sdk.get_resource_oauth2_token.call_args_list), 1)
        self.assertEqual(set(self.ctx.register_tool.call_args.kwargs["schema"]["parameters"]["properties"]), {"tool", "arguments"})
        self.sdk.complete_resource_token_auth.assert_not_called()
        self.next_listener.assert_not_awaited()

    async def test_unapproved_user_workspace_mismatch_and_untrusted_event_cannot_force(self):
        for kwargs in ({"user": "UOTHER"}, {"team": "TOTHER"}, {"trusted": False}):
            self.assertEqual((await self.dispatch("hac:reauthenticate", **kwargs)).status, 403)
        self.sdk.get_resource_oauth2_token.assert_not_called()
        self.assertEqual(self.runtime.status(self.alice), {"gateway": "connected"})
        self.next_listener.assert_not_awaited()

    async def test_legacy_middleware_does_not_treat_new_action_as_disconnect(self):
        runtime = Mock(providers={"gateway": object()})
        ui = NativeConnections(runtime, ["TTEST"])
        self.assertEqual((await self.dispatch("hac:reauthenticate", ui=ui)).status, 403)
        runtime.disconnect.assert_not_called()
        runtime.connect.assert_not_called()
        self.next_listener.assert_not_awaited()
