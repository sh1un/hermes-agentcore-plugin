"""Native portal entry, never registered as an LLM tool."""
import asyncio
import threading
import time
from slack_bolt.response import BoltResponse
from .slack_ui import NativeConnections


class PortalConnections(NativeConnections):
    def __init__(self, runtime, workspaces):
        super().__init__(runtime, workspaces)
        self.destinations = {}
        self.destination_lock = threading.Lock()

    def remember(self, identity, client):
        with self.destination_lock:
            now = time.monotonic()
            self.destinations = {k: v for k, v in self.destinations.items() if v[0] > now}
            if identity not in self.destinations and len(self.destinations) >= 256:
                raise ValueError("UI capacity reached")
            self.destinations[identity] = (now + 600, asyncio.get_running_loop(), client)

    def notify(self, identity):
        """Called by callback thread with server-resolved identity, never URL input."""
        with self.destination_lock:
            destination = self.destinations.pop(identity, None)
        if not destination or destination[0] <= time.monotonic():
            return
        _, loop, client = destination
        def enqueue():
            if len(self.tasks) >= 32:
                return
            task = asyncio.create_task(self.safe(self.home(client, identity)))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        if not loop.is_closed():
            loop.call_soon_threadsafe(enqueue)

    async def home(self, client, identity, notice="", url=None):
        status = await asyncio.to_thread(self.runtime.status, identity)
        def button(label, action, value="none", url=None):
            b = {"type": "button", "text": {"type": "plain_text", "text": label},
                 "action_id": "hacp:" + action, "value": value}
            if url:
                b["url"] = url
            return b
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": "AgentCore Connections"}},
                  {"type": "section", "text": {"type": "plain_text", "text":
                    notice or "Connect your Google account, then use the same account in the consent portal to authorize Jira."}},
                  {"type": "section", "text": {"type": "plain_text", "text":
                    {"signed_out": "Google account not connected", "signed_in": "Google account connected",
                     "confirm_identity": "Confirm your Google account"}[status["state"]]
                    + (": " + status["label"] if status.get("label") else "")}}]
        buttons = []
        if status["state"] == "signed_out":
            url = await asyncio.to_thread(self.runtime.login_url, identity)
            connect = button("Connect Google account", "open", url=url)
            connect["style"] = "primary"
            buttons.append(connect)
        if status["state"] == "confirm_identity":
            b = button("Confirm account", "confirm", status["attempt"])
            b["style"] = "primary"
            buttons.append(b)
        buttons.append(button("Refresh", "refresh"))
        signout = button("Cancel connection" if status["state"] != "signed_in" else "Sign out", "signout")
        if status["state"] == "signed_in":
            signout["style"] = "danger"
        buttons.append(signout)
        blocks.append({"type": "actions", "elements": buttons})
        blocks.append({"type": "actions", "elements": [button("Manage Connections", "portal", url=self.runtime.s["portal_url"])]})
        blocks.append({"type": "context", "elements": [{"type": "plain_text", "text":
            "Connecting Google does not authorize Jira. Signing out here leaves portal and provider grants unchanged."}]})
        await client.views_publish(user_id=identity.member, view={"type": "home", "blocks": blocks})

    async def async_process(self, *, req, resp, next):
        body = req.body
        text = body.get("event", {}).get("text", "")
        if isinstance(text, str) and any(x in text for x in ("HAC-", "/oauth2/authorize", "/oauth/cognito/callback")):
            return BoltResponse(status=200, body="")
        home = body.get("event", {}).get("type") == "app_home_opened"
        action = (body.get("actions") or [{}])[0]
        aid = action.get("action_id", "")
        # Consume old native auth buttons too, never hand them to another listener.
        if aid.startswith("hac:") or body.get("view", {}).get("callback_id") == "hac:confirm":
            return BoltResponse(status=200, body="")
        if not home and not aid.startswith("hacp:"):
            return await next()
        try:
            identity = self.identity(body, req.context)
            self.remember(identity, req.context["client"])
            if len(self.tasks) >= 32:
                raise ValueError("Busy")
            if not home and aid not in {"hacp:login", "hacp:refresh", "hacp:signout", "hacp:confirm", "hacp:open", "hacp:portal"}:
                raise ValueError("Invalid action")
            if aid in {"hacp:open", "hacp:portal"}:
                return BoltResponse(status=200, body="")
            task = asyncio.create_task(self.safe(self.perform(req.context["client"], identity, aid, action.get("value"))))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return BoltResponse(status=200, body="")
        except Exception:
            return BoltResponse(status=403, body="Login action rejected")

    async def perform(self, client, identity, action, value):
        url, notice = None, ""
        try:
            if action == "hacp:login":
                notice = "Select Connect Google account, sign in, then return to Slack to confirm the account."
            elif action == "hacp:confirm":
                await asyncio.to_thread(self.runtime.confirm, identity, value)
                notice = "Google account connected. Open Manage Connections and authorize Jira with the same account."
            elif action == "hacp:signout":
                await asyncio.to_thread(self.runtime.signout, identity)
                notice = "Signed out here. Existing portal and provider grants were not revoked."
        except Exception:
            notice = "Login action failed or expired. Start again from this page."
        await self.home(client, identity, notice, url)
