import asyncio
import json
from pathlib import Path
import time
import tomllib
import unittest
from unittest.mock import Mock, AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from agentcore_core.connections import SlackIdentity
from agentcore_core.portal import Cognito, Login, PortalRuntime, Attempt, validate_settings
from agentcore_core.portal_ui import PortalConnections


def settings():
    with open(Path(__file__).parents[1] / "examples/portal.toml", "rb") as f:
        return validate_settings(tomllib.load(f))


class ClaimsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk["kid"] = "test-key"

    def setUp(self):
        self.s = settings()
        self.backend = Cognito(self.s)
        self.base = {"iss": self.s["issuer"], "sub": "person-a", "iat": int(time.time()), "exp": int(time.time()) + 600}

    def tokens(self, access_changes=None, id_changes=None):
        access = dict(self.base, token_use="access", client_id=self.s["client_id"], scope=self.s["scope"])
        identity = dict(self.base, token_use="id", aud=self.s["client_id"], nonce="nonce", email="a@example.com", email_verified=True)
        access.update(access_changes or {})
        identity.update(id_changes or {})
        return {"access_token": jwt.encode(access, self.key, algorithm="RS256", headers={"kid": "test-key"}),
                "id_token": jwt.encode(identity, self.key, algorithm="RS256", headers={"kid": "test-key"}),
                "refresh_token": "must-not-store"}

    def test_signed_tokens_and_pkce(self):
        login = self.backend.verify(self.tokens(), {"keys": [self.jwk]}, "nonce")
        self.assertEqual(login.sub, "person-a")
        self.assertEqual(login.label, "a@example.com")
        self.assertNotIn("must-not-store", repr(login))
        a = Attempt(SlackIdentity("TEXAMPLE", "U1"), "state", "nonce", "x" * 64, time.time() + 600)
        query = parse_qs(urlsplit(self.backend.authorize(a)).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn("code_verifier", query)
        self.assertEqual(query["nonce"], ["nonce"])

    def test_wrong_claims_rejected(self):
        cases = [({"iss": "https://evil.example"}, {}), ({"client_id": "wrong"}, {}),
                 ({"token_use": "id"}, {}), ({"scope": "openid"}, {}),
                 ({"sub": "other"}, {}), ({"exp": 1}, {}),
                 ({}, {"nonce": "wrong"}), ({}, {"aud": "wrong"}), ({}, {"token_use": "access"})]
        for access, identity in cases:
            with self.subTest(access=access, identity=identity), self.assertRaises(Exception):
                self.backend.verify(self.tokens(access, identity), {"keys": [self.jwk]}, "nonce")

    def test_wrong_signature_rejected(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        tokens = self.tokens()
        tokens["access_token"] = jwt.encode(dict(self.base, token_use="access"), other, algorithm="RS256", headers={"kid": "test-key"})
        with self.assertRaises(Exception):
            self.backend.verify(tokens, {"keys": [self.jwk]}, "nonce")

    def test_exchange_uses_public_client_pkce_and_fixed_endpoints(self):
        a = Attempt(SlackIdentity("TEXAMPLE", "U1"), "state", "nonce", "verifier", time.time() + 600)
        client = Mock()
        client.post.return_value.json.return_value = self.tokens()
        client.get.return_value.json.return_value = {"keys": [self.jwk]}
        with patch("httpx.Client") as factory:
            factory.return_value.__enter__.return_value = client
            login = self.backend.exchange(a, "test-code")
        self.assertEqual(login.sub, "person-a")
        self.assertFalse(factory.call_args.kwargs["follow_redirects"])
        self.assertEqual(client.post.call_args.args[0], self.s["cognito_domain"] + "/oauth2/token")
        data = client.post.call_args.kwargs["data"]
        self.assertEqual(data["code_verifier"], "verifier")
        self.assertNotIn("client_secret", data)
        self.assertNotIn("refresh", repr(login))


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.now = 1000
        self.backend = Mock()
        self.backend.authorize.side_effect = lambda a: "https://login.example/?state=" + a.state
        self.backend.exchange.side_effect = lambda a, c: Login(c, c, "token-" + c, self.now + 300)
        self.transport = Mock(return_value={"isError": False, "content": [{"type": "text", "text": "ok"}]})
        self.r = PortalRuntime(self.s, self.backend, self.transport, lambda: self.now)
        self.a, self.b = SlackIdentity("TEXAMPLE", "U1"), SlackIdentity("TEXAMPLE", "U2")

    def login(self, person, account):
        state = parse_qs(urlsplit(self.r.start(person)).query)["state"][0]
        self.r.callback(state, account)
        attempt = self.r.status(person)["attempt"]
        return state, attempt

    def call(self, person):
        return self.r.execute(person, "getAccessibleAtlassianResources", {})

    def test_native_direct_login_reuses_link_and_pushes_confirmation(self):
        async def scenario():
            ui = PortalConnections(self.r, self.s["workspace_ids"])
            client = AsyncMock()
            ui.remember(self.a, client)
            await ui.home(client, self.a)
            blocks = client.views_publish.call_args.kwargs["view"]["blocks"]
            buttons = [b for block in blocks for b in block.get("elements", []) if b.get("type") == "button"]
            connect = next(b for b in buttons if b["text"]["text"] == "連接 Google 帳號")
            self.assertEqual(connect["action_id"], "hacp:open")
            self.assertEqual(connect["style"], "primary")
            self.assertNotIn("agent_prompt", connect)
            url = connect["url"]
            await ui.home(client, self.a)
            self.assertEqual(self.r.login_url(self.a), url)
            state = parse_qs(urlsplit(url).query)["state"][0]
            self.assertNotEqual(self.r.login_url(self.b), url)
            identity = await asyncio.to_thread(self.r.callback, state, "my-account")
            self.assertEqual(identity, self.a)
            self.assertEqual(self.call(self.a)["error"], "sign_in_required")
            updated = asyncio.Event()
            async def published(**kwargs):
                self.assertEqual(kwargs["user_id"], self.a.member)
                updated.set()
            client.views_publish.side_effect = published
            await asyncio.to_thread(ui.notify, identity)
            await asyncio.wait_for(updated.wait(), 2)
            blocks = client.views_publish.call_args.kwargs["view"]["blocks"]
            buttons = [b for block in blocks for b in block.get("elements", []) if b.get("type") == "button"]
            confirm = next(b for b in buttons if b["action_id"] == "hacp:confirm")
            self.assertEqual(confirm["style"], "primary")
            refresh = next(b for b in buttons if b["action_id"] == "hacp:refresh")
            self.assertEqual(refresh["text"]["text"], "重新整理")
            self.assertNotEqual(confirm["value"], state)
            self.assertNotIn("confirm", confirm)  # One explicit confirmation, no second dialog.
            with self.assertRaises(ValueError):
                self.r.confirm(self.b, confirm["value"])
            self.r.confirm(self.a, confirm["value"])
            self.assertEqual(self.r.status(self.a)["state"], "signed_in")
            await ui.home(client, self.a)
            blocks = client.views_publish.call_args.kwargs["view"]["blocks"]
            buttons = [b for block in blocks for b in block.get("elements", []) if b.get("type") == "button"]
            signout = next(b for b in buttons if b["action_id"] == "hacp:signout")
            self.assertEqual(signout["style"], "danger")
            self.assertEqual(signout["text"]["text"], "登出此 Plugin")
        asyncio.run(scenario())

    def test_direct_login_expiry_and_cancellation(self):
        first = self.r.login_url(self.a)
        self.r.signout(self.a)
        with self.assertRaises(ValueError):
            self.r.callback(parse_qs(urlsplit(first).query)["state"][0], "code")
        second = self.r.login_url(self.a)
        self.now += 601
        self.assertNotEqual(second, self.r.login_url(self.a))

    def test_two_users_and_local_signout(self):
        for person, account in [(self.a, "a"), (self.b, "b")]:
            _, attempt = self.login(person, account)
            self.assertEqual(self.call(person)["error"], "sign_in_required")
            self.r.confirm(person, attempt)
            self.assertIn("result", self.call(person))
            self.assertEqual(self.transport.call_args.args[1], "token-" + account)
            self.assertEqual(self.transport.call_args.args[0].endpoint, self.s["gateway_url"])
        self.r.signout(self.a)
        self.assertEqual(self.call(self.a)["error"], "sign_in_required")
        self.assertIn("result", self.call(self.b))

    def test_callback_and_confirmation_replay(self):
        state, key = self.login(self.a, "a")
        for bad in [state, "wrong-state"]:
            with self.assertRaises(ValueError):
                self.r.callback(bad, "a")
        with self.assertRaises(ValueError):
            self.r.confirm(self.b, key)
        self.r.confirm(self.a, key)
        with self.assertRaises(ValueError):
            self.r.confirm(self.a, key)

    def test_cancel_and_expiry(self):
        _, key = self.login(self.a, "a")
        self.r.signout(self.a)
        with self.assertRaises(ValueError):
            self.r.confirm(self.a, key)
        _, key = self.login(self.a, "a")
        self.r.confirm(self.a, key)
        self.now += 301
        self.assertEqual(self.call(self.a)["error"], "sign_in_required")
        self.assertFalse(self.r.logins)

    def test_new_attempt_invalidates_old_confirmation(self):
        _, key = self.login(self.a, "a")
        self.r.start(self.a)
        with self.assertRaises(ValueError):
            self.r.confirm(self.a, key)

    def test_models_do_not_trigger_login_or_receive_auth(self):
        self.assertEqual(self.call(self.a)["error"], "sign_in_required")
        self.backend.authorize.assert_not_called()
        self.transport.assert_not_called()
        _, key = self.login(self.a, "a")
        self.r.confirm(self.a, key)
        for result in [{"isError": True, "secret": "https://login/?code=hidden"},
                       {"authorizationUrl": "hidden"}, {"content": [{"text": "token-a"}]}]:
            self.transport.return_value = result
            safe = json.dumps(self.call(self.a))
            self.assertNotIn("hidden", safe)
            self.assertNotIn("token-a", safe)
        self.transport.side_effect = RuntimeError("code=hidden")
        self.assertNotIn("hidden", json.dumps(self.call(self.a)))

    def test_arguments_and_workspace_isolation(self):
        for tool, args in [("createJiraIssue", {}), ("getJiraIssue", {"subject": "other"}), ([], {})]:
            self.assertEqual(self.r.execute(self.a, tool, args)["error"], "invalid_arguments")
        with self.assertRaises(ValueError):
            self.r.start(SlackIdentity("TOTHER", "U1"))

    def test_callback_failure_consumes_state(self):
        state = parse_qs(urlsplit(self.r.start(self.a)).query)["state"][0]
        self.backend.exchange.side_effect = ValueError("unsafe provider error")
        with self.assertRaises(ValueError):
            self.r.callback(state, "code")
        self.assertNotIn(state, self.r.pending)
        self.assertEqual(self.r.status(self.a)["state"], "signed_out")

    def test_restart_loses_login_and_pending(self):
        _, key = self.login(self.a, "a")
        self.r.confirm(self.a, key)
        self.r.close()
        self.assertEqual(self.call(self.a)["error"], "sign_in_required")
        self.assertFalse(self.r.pending)


class PortalHostTests(unittest.TestCase):
    def test_http_callback_never_echoes_code_or_error(self):
        from urllib.request import build_opener, ProxyHandler
        from urllib.error import HTTPError
        from agentcore_core.callback import start_callback
        runtime = Mock()
        # Test local HTTP only, without macOS system proxy discovery or DNS.
        opener = build_opener(ProxyHandler({}))
        urlopen = opener.open
        on_complete = Mock()
        with patch("socket.getfqdn", return_value="localhost"):
            server = start_callback(runtime, 0, portal=True, on_complete=on_complete)
        try:
            url = f"http://127.0.0.1:{server.server_port}/oauth/cognito/callback"
            with urlopen(url + "?state=private-state&code=private-code", timeout=3) as response:
                body = response.read().decode()
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertNotIn("private", body)
                self.assertIn("Slack", body)
            runtime.callback.assert_called_once_with("private-state", "private-code")
            on_complete.assert_called_once_with(runtime.callback.return_value)
            with self.assertRaises(HTTPError) as caught:
                urlopen(url + "?state=a&state=b&code=c", timeout=3)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
            runtime.callback.side_effect = ValueError("private-token")
            with self.assertRaises(HTTPError) as caught:
                urlopen(url + "?state=a&code=c", timeout=3)
            self.assertNotIn(b"private-token", caught.exception.read())
            self.assertEqual(on_complete.call_count, 1)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_host_registers_only_native_login_and_read_tool(self):
        import sys
        import types
        from contextvars import ContextVar
        from agentcore_core.host import enable_portal
        gateway = types.ModuleType("gateway")
        gateway.session_context = types.SimpleNamespace(_VAR_MAP={k: ContextVar(k) for k in
            ["HERMES_SESSION_PLATFORM", "HERMES_SESSION_SCOPE_ID", "HERMES_SESSION_USER_ID", "HERMES_CRON_SESSION"]})
        ctx = Mock()
        with patch.dict(sys.modules, {"gateway": gateway}), patch("atexit.register"), patch("boto3.client") as aws:
            runtime = enable_portal(ctx, settings())
        aws.assert_not_called()
        ctx.register_platform_handler.assert_called_once()
        ctx.register_tool.assert_called_once()
        definition = ctx.register_tool.call_args.kwargs
        self.assertEqual(definition["name"], "agentcore_jira_read")
        self.assertEqual(set(definition["schema"]["parameters"]["properties"]), {"tool", "arguments"})
        self.assertEqual(json.loads(definition["handler"]({"tool": "getJiraIssue", "arguments": {}, "token": "secret"}))["error"], "invalid_arguments")
        runtime.close()


class PortalUITests(unittest.IsolatedAsyncioTestCase):
    async def test_native_confirm_uses_authenticated_slack_user(self):
        from slack_bolt.request.async_request import AsyncBoltRequest
        from slack_bolt.response import BoltResponse
        runtime = Mock()
        runtime.s = settings()
        ui = PortalConnections(runtime, ["TEXAMPLE"])
        ui.home = AsyncMock()
        nxt = AsyncMock()
        req = AsyncBoltRequest(body={"type": "block_actions", "team": {"id": "TEXAMPLE"},
            "user": {"id": "U1"}, "actions": [{"action_id": "hacp:confirm", "value": "attempt"}]}, mode="socket_mode")
        req.context.update(team_id="TEXAMPLE", authorize_result=object(), client=Mock())
        response = await ui.async_process(req=req, resp=BoltResponse(status=200), next=nxt)
        self.assertEqual(response.status, 200)
        await asyncio.gather(*list(ui.tasks))
        runtime.confirm.assert_called_once_with(SlackIdentity("TEXAMPLE", "U1"), "attempt")
        nxt.assert_not_awaited()
