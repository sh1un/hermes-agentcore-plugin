"""Native portal entry, never registered as an LLM tool."""
import asyncio
from slack_bolt.response import BoltResponse
from .slack_ui import NativeConnections


class PortalConnections(NativeConnections):
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
                    status["state"] + (": " + status["label"] if status.get("label") else "")}}]
        buttons = [button("連接 Google 帳號", "login"), button("重新整理", "refresh"), button("登出此 Plugin", "signout")]
        if url:
            buttons.append(button("前往 Google 帳號登入", "open", url=url))
        if status["state"] == "confirm_identity":
            b = button("確認連接此帳號", "confirm", status["attempt"])
            b["confirm"] = {"title": {"type": "plain_text", "text": "Link your account?"},
                "text": {"type": "plain_text", "text": "Only confirm if this is your account: " + status["label"]},
                "confirm": {"type": "plain_text", "text": "Confirm"},
                "deny": {"type": "plain_text", "text": "Cancel"}}
            buttons.append(b)
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
                url = await asyncio.to_thread(self.runtime.start, identity)
                notice = "點選「前往 Google 帳號登入」，登入後回到這裡點「重新整理」，再確認連接此帳號。"
            elif action == "hacp:confirm":
                await asyncio.to_thread(self.runtime.confirm, identity, value)
                notice = "Google 帳號已連接。請開啟 Manage Connections，使用同一帳號授權 Jira。"
            elif action == "hacp:signout":
                await asyncio.to_thread(self.runtime.signout, identity)
                notice = "Signed out here. Existing portal and provider grants were not revoked."
        except Exception:
            notice = "Login action failed or expired. Start again from this page."
        await self.home(client, identity, notice, url)
