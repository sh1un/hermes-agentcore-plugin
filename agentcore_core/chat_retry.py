"""Native Slack prompts for user-initiated Jira issue reads."""
import asyncio
from collections import OrderedDict
from contextvars import copy_context
from dataclasses import dataclass
import re
import secrets
from threading import Lock
import time

from slack_bolt.response import BoltResponse


ISSUE = re.compile(r"(?<![\w-])([A-Z][A-Z0-9_]*-\d+)(?![\w-])", re.ASCII)


@dataclass(frozen=True)
class Origin:
    identity: object
    channel: str
    message: str
    thread: str
    issue: str

    @property
    def key(self):
        return self.identity, self.channel, self.message


class ChatRetry:
    def __init__(self, runtime, adapter):
        self.runtime = runtime
        self.adapter = adapter
        self.origins = OrderedDict()
        self.lock = Lock()
        # Only the Slack event loop accesses tickets and background tasks.
        self.tickets = {}
        self.tasks = set()

    def observe(self, identity, body, client):
        event = body.get("event", {})
        if event.get("user") != identity.member:
            return
        if event.get("type") not in ("message", "app_mention") or event.get("subtype") or event.get("bot_id"):
            return
        keys = set(ISSUE.findall(event.get("text", "")))
        if len(keys) != 1 or not event.get("channel") or not event.get("ts"):
            return
        origin = Origin(identity, event["channel"], event["ts"], event.get("thread_ts", ""), keys.pop())
        with self.lock:
            self.origins[origin.key] = (time.monotonic() + 1200, origin, client, asyncio.get_running_loop())
            self.origins.move_to_end(origin.key)
            while len(self.origins) > 512:
                self.origins.popitem(last=False)

    def lookup(self, identity):
        from gateway import session_context
        context = copy_context()
        def field(key):
            var = session_context._VAR_MAP.get("HERMES_SESSION_" + key)
            return context.get(var) if var is not None else None
        with self.lock:
            entry = self.origins.get((identity, field("CHAT_ID"), field("MESSAGE_ID")))
        if entry and entry[0] > time.monotonic():
            _, origin, client, loop = entry
            thread = field("THREAD_ID") or ""
            if thread == origin.thread or (not origin.thread and thread == origin.message):
                return origin, client, loop
        return None

    def execute(self, identity, tool, arguments):
        result = self.runtime.execute(identity, "gateway", tool, arguments)
        error = result.get("error")
        if error not in ("connection_required", "tool_authorization_required", "service_unavailable"):
            return result
        match = self.lookup(identity)
        if not match:
            return result
        origin, client, loop = match
        if tool == "getJiraIssue" and arguments.get("issueIdOrKey") != origin.issue:
            return result
        url = None
        if error == "connection_required":
            try:
                url = self.runtime.connect(identity, "gateway")
            except Exception:
                return {"error": "service_unavailable", "message": "Open app Home to check sign-in."}
            if not url:
                result = self.runtime.execute(identity, "gateway", tool, arguments)
                error = result.get("error")
                if error not in ("tool_authorization_required", "service_unavailable"):
                    return result
        if error == "tool_authorization_required":
            url = self.runtime.portal_url
        if loop.is_closed():
            return result
        loop.call_soon_threadsafe(self.background, self.prompt(origin, client, error, url))
        return {"status": "action_required", "message": "Use the private Slack prompt to retry this issue. If it is missing, open app Home and ask again."}

    def background(self, coroutine):
        if len(self.tasks) >= 32:
            coroutine.close()
            return
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())

    async def private(self, client, origin, **payload):
        route = {"channel": origin.channel}
        if origin.thread:
            route["thread_ts"] = origin.thread
        if origin.channel.startswith("D"):
            return await client.chat_postMessage(**route, **payload)
        return await client.chat_postEphemeral(**route, user=origin.identity.member, **payload)

    async def prompt(self, origin, client, error, url):
        now = time.monotonic()
        for key, (expires, _, _) in list(self.tickets.items()):
            if expires <= now:
                self.tickets.pop(key, None)
        if len(self.tickets) >= 512:
            return
        token = secrets.token_urlsafe(24)
        buttons = []
        if url:
            label = "Connect Jira" if error == "tool_authorization_required" else "Sign in"
            buttons.append({"type": "button", "text": {"type": "plain_text", "text": label},
                            "style": "primary", "action_id": "hac:open", "url": url})
            message = f"{label}, then retry {origin.issue}."
        else:
            message = f"The gateway is unavailable. Retry {origin.issue} in a moment."
        buttons.append({"type": "button", "text": {"type": "plain_text", "text": "Retry lookup"},
                        "action_id": "hac:retry_issue", "value": token})
        reply = await self.private(client, origin, text=message, blocks=[
            {"type": "section", "text": {"type": "plain_text", "text": message}},
            {"type": "actions", "elements": buttons}])
        ts = reply.get("message_ts") or reply.get("ts")
        if ts:
            for old, (_, previous, _) in list(self.tickets.items()):
                if previous.key == origin.key:
                    del self.tickets[old]
            self.tickets[token] = (now + 1200, origin, ts)

    async def click(self, identity, body, client):
        action = body["actions"][0]
        token = action.get("value")
        if not isinstance(token, str) or not isinstance(action.get("action_ts"), str):
            return BoltResponse(status=403, body="")
        ticket = self.tickets.get(token)
        container = body.get("container", {})
        channel = body.get("channel", {}).get("id")
        if not ticket or ticket[0] <= time.monotonic():
            if channel:
                self.background(client.chat_postEphemeral(channel=channel, user=identity.member,
                    text="This retry has expired or was already used. Ask for the issue again."))
            return BoltResponse(status=200, body="")
        _, origin, prompt_ts = ticket
        if identity != origin.identity or channel != origin.channel or container.get("message_ts") != prompt_ts:
            return BoltResponse(status=403, body="")
        if len(self.tasks) >= 32:
            return BoltResponse(status=429, body="Busy")
        del self.tickets[token]
        self.background(self.retry(origin, client, action.get("action_ts")))
        return BoltResponse(status=200, body="")

    async def retry(self, origin, client, action_ts):
        try:
            is_dm = origin.channel.startswith("D")
            if self.adapter._early_reject_unauthorized(origin.identity.member, origin.channel, is_dm,
                                                       thread_id=origin.thread or None):
                await self.private(client, origin, text="This conversation no longer allows this request.")
                return
            state = await asyncio.to_thread(self.runtime.status, origin.identity)
            url = None
            if state["gateway"] != "connected":
                url = await asyncio.to_thread(self.runtime.connect, origin.identity, "gateway")
            if url:
                await self.prompt(origin, client, "connection_required", url)
                return
            # Resume a narrow read through the same host as an ordinary user turn.
            # Never replay arbitrary text or inherit elevated/internal event flags.
            from gateway.platforms.base import MessageEvent
            source = self.adapter.build_source(chat_id=origin.channel,
                chat_type="dm" if origin.channel.startswith("D") else "group",
                thread_id=origin.thread or (None if origin.channel.startswith("D") else origin.message),
                user_id=origin.identity.member, scope_id=origin.identity.workspace, message_id=action_ts)
            text = f"Use agentcore_jira_read to read Jira issue {origin.issue} with my identity."
            synthetic = {"event": {"type": "message", "user": origin.identity.member, "text": text, "ts": action_ts,
                                    "channel": origin.channel, "thread_ts": source.thread_id or ""}}
            self.observe(origin.identity, synthetic, client)
            await self.adapter.handle_message(MessageEvent(text=text, source=source, user_id=origin.identity.member, message_id=action_ts))
        except Exception:
            await self.prompt(origin, client, "service_unavailable", None)
