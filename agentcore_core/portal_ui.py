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
                    notice or "先連接你的 Google 帳號，讓 Plugin 知道這個 Slack 身份對應哪個登入帳號，再到 Consent portal 使用同一帳號授權 Jira。"}},
                  {"type": "section", "text": {"type": "plain_text", "text":
                    {"signed_out": "尚未連接 Google 帳號", "signed_in": "已連接 Google 帳號",
                     "confirm_identity": "請確認這是你的 Google 帳號"}[status["state"]]
                    + (": " + status["label"] if status.get("label") else "")}}]
        buttons = []
        if status["state"] == "signed_out":
            url = await asyncio.to_thread(self.runtime.login_url, identity)
            connect = button("連接 Google 帳號", "open", url=url)
            connect["style"] = "primary"
            buttons.append(connect)
        if status["state"] == "confirm_identity":
            b = button("確認連接此帳號", "confirm", status["attempt"])
            b["style"] = "primary"
            buttons.append(b)
        buttons.append(button("更新狀態", "refresh"))
        signout = button("取消連接" if status["state"] != "signed_in" else "登出此 Plugin", "signout")
        if status["state"] == "signed_in":
            signout["style"] = "danger"
        buttons.append(signout)
        blocks.append({"type": "actions", "elements": buttons})
        blocks.append({"type": "actions", "elements": [button("Manage Connections", "portal", url=self.runtime.s["portal_url"])]})
        blocks.append({"type": "context", "elements": [{"type": "plain_text", "text":
            "連接 Google 帳號不代表已授權 Jira。登出此 Plugin 不會撤銷 Consent portal 或第三方服務的授權。"}]})
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
                notice = "請點「連接 Google 帳號」直接開啟登入頁，登入後回到 Slack 確認帳號。"
            elif action == "hacp:confirm":
                await asyncio.to_thread(self.runtime.confirm, identity, value)
                notice = "Google 帳號已連接。請開啟 Manage Connections，使用同一帳號授權 Jira。"
            elif action == "hacp:signout":
                await asyncio.to_thread(self.runtime.signout, identity)
                notice = "Signed out here. Existing portal and provider grants were not revoked."
        except Exception:
            notice = "Login action failed or expired. Start again from this page."
        await self.home(client, identity, notice, url)
