"""Slack native middleware, consumed before conversation listeners and history."""
import asyncio
from slack_bolt.middleware.async_middleware import AsyncMiddleware
from slack_bolt.response import BoltResponse

from .connections import SlackIdentity


class NativeConnections(AsyncMiddleware):
    def __init__(self, runtime, workspaces):
        self.runtime, self.workspaces = runtime, frozenset(workspaces)
        self.tasks = set()

    def identity(self, body, context):
        team = body.get("team_id") or body.get("team", {}).get("id")
        user = body.get("user")
        user = user.get("id") if isinstance(user, dict) else user
        user = user or body.get("event", {}).get("user")
        # Bolt middleware has already authenticated the installation. Reject mismatch.
        if (team not in self.workspaces or context.get("team_id") != team
                or not context.get("authorize_result")):
            raise ValueError("Untrusted installation")
        return SlackIdentity(team, user)

    async def home(self, client, identity, notice="", url=None, active=None):
        states = await asyncio.to_thread(self.runtime.status, identity)
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": "Connections"}}]
        if notice:
            blocks.append({"type": "section", "text": {"type": "plain_text", "text": notice}})
        for name, state in states.items():
            blocks.append({"type": "section", "text": {"type": "plain_text",
                "text": f"{name}: {state}"}})
            buttons = []
            actions = ["connect"] if state == "disconnected" else ["connect", "code", "disconnect"] if state == "connecting" else ["disconnect"]
            for action in actions:
                buttons.append({"type": "button", "text": {"type": "plain_text",
                    "text": {"connect": "Connect / Resume", "code": "Enter code", "disconnect": "Disconnect"}[action]},
                    "action_id": f"hac:{action}", "value": name})
            if url and active == name and state == "connecting":
                buttons.append({"type": "button", "text": {"type": "plain_text", "text": "Authorize"},
                                "url": url, "action_id": "hac:open", "value": name})
            blocks.append({"type": "actions", "elements": buttons})
        await client.views_publish(user_id=identity.member, view={"type": "home", "blocks": blocks})

    async def async_process(self, *, req, resp, next):
        body = req.body
        # Consume recognizable auth artifacts pasted into ordinary chat locally.
        text = body.get("event", {}).get("text", "")
        if isinstance(text, str) and ("HAC-" in text or "identities/oauth2/authorize" in text):
            return BoltResponse(status=200, body="")
        home = body.get("event", {}).get("type") == "app_home_opened"
        actions = body.get("actions", [])
        action = actions[0] if actions else {}
        aid = action.get("action_id", "")
        view = body.get("view", {})
        submission = body.get("type") == "view_submission" and view.get("callback_id") == "hac:confirm"
        if not home and not aid.startswith("hac:") and not submission:
            return await next()
        try:
            identity = self.identity(body, req.context)
            if len(self.tasks) >= 32:
                raise ValueError("Busy")
            client = req.context["client"]
            if submission:
                name = view.get("private_metadata")
                code = view["state"]["values"]["code"]["code"]["value"]
                if name not in self.runtime.providers or not isinstance(code, str) or not code.startswith("HAC-"):
                    return BoltResponse(status=200, body={"response_action": "errors", "errors": {"code": "Enter the callback confirmation code"}})
                work = self.confirm(client, identity, name, code)
            elif home:
                work = self.home(client, identity)
            elif aid == "hac:open":
                return BoltResponse(status=200, body="")
            else:
                name = action.get("value")
                if name not in self.runtime.providers or aid not in {"hac:connect", "hac:code", "hac:disconnect"}:
                    raise ValueError("Invalid action")
                work = self.action(client, identity, name, aid, body.get("trigger_id"))
            task = asyncio.create_task(self.safe(work))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return BoltResponse(status=200, body="")
        except Exception:
            return BoltResponse(status=403, body="Connection action rejected")

    async def safe(self, work):
        try:
            await work
        except Exception:
            # Never let Slack/Bolt serialize exceptions containing OAuth data.
            pass

    async def action(self, client, identity, name, action, trigger):
        if action == "hac:code":
            await client.views_open(trigger_id=trigger, view={"type": "modal", "callback_id": "hac:confirm",
                "private_metadata": name, "title": {"type": "plain_text", "text": "Confirm connection"},
                "submit": {"type": "plain_text", "text": "Confirm"},
                "blocks": [{"type": "input", "block_id": "code", "label": {"type": "plain_text", "text": "One-time code"},
                            "element": {"type": "plain_text_input", "action_id": "code", "max_length": 128}}]})
            return
        try:
            if action == "hac:connect":
                url = await asyncio.to_thread(self.runtime.connect, identity, name)
                await self.home(client, identity, "Continue authorization, then enter the callback code" if url else "Connected", url, name)
            else:
                await asyncio.to_thread(self.runtime.disconnect, identity, name)
                await self.home(client, identity, "Disconnected here. Provider-side token revocation is not supported by this plugin.")
        except Exception:
            await self.home(client, identity, "Connection action failed. Try again from this page.")

    async def confirm(self, client, identity, name, code):
        try:
            await asyncio.to_thread(self.runtime.confirm, identity, name, code)
            notice = "Connected. You can retry your original request."
        except Exception:
            notice = "Confirmation failed or expired. Check the code or start a new connection."
        await self.home(client, identity, notice)
