"""Native portal entry, never registered as an LLM tool."""
import asyncio
import json
import threading
import time
from slack_bolt.response import BoltResponse
from .slack_ui import NativeConnections
from .connections import SlackIdentity


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
        identity = SlackIdentity(identity.workspace, identity.member)
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
            personal_url = await asyncio.to_thread(self.runtime.login_url, identity)
            connect = button("連接 Google 帳號", "open", url=personal_url)
            connect["style"] = "primary"
            buttons.append(connect)
        if status["state"] == "confirm_identity":
            b = button("確認連接此帳號", "confirm", status["attempt"])
            b["style"] = "primary"
            buttons.append(b)
        buttons.append(button("重新整理", "refresh"))
        signout = button("取消連接" if status["state"] != "signed_in" else "登出此 Plugin", "signout")
        if status["state"] == "signed_in":
            signout["style"] = "danger"
        buttons.append(signout)
        blocks.append({"type": "actions", "elements": buttons})
        blocks.append({"type": "actions", "elements": [button("Manage Connections", "portal", url=self.runtime.s["portal_url"])]})
        blocks.append({"type": "context", "elements": [{"type": "plain_text", "text":
            "連接 Google 帳號不代表已授權 Jira。登出此 Plugin 不會撤銷 Consent portal 或第三方服務的授權。"}]})
        for name in self.runtime.managed_services(identity):
            owner = self.runtime.service_owner(identity, name)
            service_status = await asyncio.to_thread(self.runtime.status, owner)
            blocks.append({"type": "divider"})
            blocks.append({"type": "section", "text": {"type": "plain_text", "text":
                "Service account: " + name + "\n" + service_status["state"]
                + (": " + service_status["label"] if service_status.get("label") else "")
                + "\n請使用指定服務帳號登入，不要使用你的個人帳號。"}})
            value = json.dumps({"service": name})
            service_buttons = []
            if service_status["state"] == "signed_out":
                service_url = await asyncio.to_thread(self.runtime.login_url, owner)
                b = button("連接服務帳號", "open", value, service_url)
                b["style"] = "primary"
                service_buttons.append(b)
            elif service_status["state"] == "confirm_identity":
                b = button("確認服務帳號", "service_confirm", json.dumps({
                    "service": name, "attempt": service_status["attempt"]}))
                b["style"] = "primary"
                service_buttons.append(b)
            b = button("登出服務連線", "service_signout", value)
            b["style"] = "danger"
            b["confirm"] = {"title": {"type": "plain_text", "text": "登出服務連線？"},
                "text": {"type": "plain_text", "text": "所有綁定此服務身份的頻道將停止查詢，直到管理員重新連接。這不會撤銷第三方授權。"},
                "confirm": {"type": "plain_text", "text": "登出"}, "deny": {"type": "plain_text", "text": "取消"}}
            service_buttons.append(b)
            service_buttons.append(button("服務帳號 Consent portal", "portal", url=self.runtime.s["portal_url"]))
            blocks.append({"type": "actions", "elements": service_buttons})
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
            if not home and aid not in {"hacp:login", "hacp:refresh", "hacp:signout", "hacp:confirm", "hacp:open", "hacp:portal", "hacp:service_login", "hacp:service_confirm", "hacp:service_signout"}:
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
            elif action in {"hacp:service_login", "hacp:service_confirm", "hacp:service_signout"}:
                data = json.loads(value)
                expected = {"service", "attempt"} if action == "hacp:service_confirm" else {"service"}
                if not isinstance(data, dict) or set(data) != expected:
                    raise ValueError("Invalid service action")
                owner = self.runtime.service_owner(identity, data["service"])
                if action == "hacp:service_login":
                    link = await asyncio.to_thread(self.runtime.login_url, owner)
                    url = {"service": data["service"], "url": link}
                    notice = "請開啟服務帳號登入，使用指定帳號。登入錯誤帳號會被拒絕。"
                elif action == "hacp:service_confirm":
                    await asyncio.to_thread(self.runtime.confirm, owner, data["attempt"])
                    notice = "服務身份已連接。請在 Consent portal 使用同一服務帳號授權第三方連線。"
                else:
                    await asyncio.to_thread(self.runtime.signout, owner)
                    notice = "服務連線已登出。頻道查詢已停用，第三方授權未撤銷。"
        except Exception:
            notice = "Login action failed or expired. Start again from this page."
        await self.home(client, identity, notice, url)
