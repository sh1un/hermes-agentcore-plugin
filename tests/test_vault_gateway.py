import asyncio
import json
from pathlib import Path
import tempfile
import types
import sys
import unittest
from unittest.mock import Mock, patch

from botocore.credentials import Credentials
from botocore.session import get_session
from botocore.validate import validate_parameters

from agentcore_core.connections import Store, SlackIdentity, ConnectionRejected
from agentcore_core.vault_gateway import CognitoVault, GatewayRuntime, Provider, SCOPES, validate_settings
from agentcore_core.transport import ToolAuthorizationRequired


def settings():
    return validate_settings(dict(mode="vault_gateway", region="ap-southeast-1", workload="example-hermes",
        credential_provider="example-cognito", agent_role_arn="arn:aws:iam::123456789012:role/example-hermes-access",
        binding_api_url="https://bindingapi.execute-api.ap-southeast-1.amazonaws.com/bindings/requests",
        return_url="https://bindingapi.execute-api.ap-southeast-1.amazonaws.com/oauth/agentcore/callback",
        gateway_url="https://example-gateway.gateway.bedrock-agentcore.ap-southeast-1.amazonaws.com/mcp",
        portal_url="https://portal.example", workspace_ids=["TTEST"], allowed_users=["UALICE", "UBOB"],
        data_dir="/tmp/hermes-vault", custom_parameters={"identity_provider": "Google"}))


class VaultBackendTests(unittest.TestCase):
    def setUp(self):
        self.session, self.http = Mock(), Mock()
        self.client = self.session.client.return_value
        self.client.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "private-workload"}
        self.backend = CognitoVault(settings(), Mock(return_value=self.session), self.http)
        self.person = SlackIdentity("TTEST", "UALICE")
        self.provider = Provider("gateway", settings()["credential_provider"], settings()["gateway_url"], SCOPES)

    def test_user_bound_token_requests_and_stable_cache_parameters(self):
        self.backend.token(self.person, self.provider, state="a" * 43)
        initial = self.client.get_resource_oauth2_token.call_args.kwargs.copy()
        self.backend.token(self.person, self.provider)
        initial.pop("customState")
        self.assertEqual(initial, self.client.get_resource_oauth2_token.call_args.kwargs)
        self.assertEqual(initial["customParameters"], {"identity_provider": "Google"})
        self.backend.token(self.person, self.provider, session="urn:ietf:params:oauth:request_uri:pending")
        self.assertIn("sessionUri", self.client.get_resource_oauth2_token.call_args.kwargs)
        model = get_session().get_service_model("bedrock-agentcore")
        for method, operation in [(self.client.get_workload_access_token_for_user_id, "GetWorkloadAccessTokenForUserId"),
                                   (self.client.get_resource_oauth2_token, "GetResourceOauth2Token")]:
            for call in method.call_args_list:
                validate_parameters(call.kwargs, model.operation_model(operation).input_shape)
        self.assertEqual(self.client.get_workload_access_token_for_user_id.call_args.kwargs["userId"], "slack:TTEST:UALICE")

    def test_registration_is_sigv4_signed_and_cannot_complete_binding(self):
        self.session.get_credentials.return_value.get_frozen_credentials.return_value = Credentials("access", "secret", "session").get_frozen_credentials()
        self.http.post.return_value.status_code = 201
        uri = "urn:ietf:params:oauth:request_uri:pending"
        self.backend.register_binding(self.person, uri, "a" * 43)
        call = self.http.post.call_args
        self.assertEqual(call.args[0], settings()["binding_api_url"])
        self.assertTrue(call.kwargs["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256"))
        self.assertIn("execute-api", call.kwargs["headers"]["Authorization"])
        self.assertEqual(json.loads(call.kwargs["content"]), {"user_id": self.person.subject, "session_uri": uri, "state": "a" * 43})
        with self.assertRaises(ConnectionRejected):
            self.backend.complete(self.person, uri)
        self.client.complete_resource_token_auth.assert_not_called()

    def test_configuration_rejects_legacy_role_or_direct_cognito_callback(self):
        for key, value in [("binding_role_arn", "arn:aws:iam::123456789012:role/binder"),
                           ("return_url", "https://other.example/oauth/agentcore/callback"),
                           ("gateway_url", "https://mcp.atlassian.com/v1/mcp/authv2"),
                           ("custom_parameters", {"userId": "other"}), ("allowed_users", [])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_settings(dict(settings(), **{key: value}))


class GatewayRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "state.sqlite", "vault-test", frozenset({"gateway"}))
        self.addCleanup(self.store.close)
        self.backend = Mock()
        self.transport = Mock(return_value={"content": [{"text": "issue data"}]})
        self.now = [0]
        self.runtime = GatewayRuntime(self.store, self.backend, self.transport,
            [Provider("gateway", "provider", settings()["gateway_url"], SCOPES)], clock=lambda: self.now[0],
            portal_url=settings()["portal_url"])
        self.alice, self.bob = SlackIdentity("TTEST", "UALICE"), SlackIdentity("TTEST", "UBOB")
        self.uri = "urn:ietf:params:oauth:request_uri:pending"
        self.url = "https://bedrock-agentcore.ap-southeast-1.amazonaws.com/identities/oauth2/authorize?request_uri=" + self.uri
        self.backend.token.return_value = {"authorizationUrl": self.url, "sessionUri": self.uri}

    def test_external_binding_then_refresh_and_user_isolation(self):
        self.assertEqual(self.runtime.connect(self.alice, "gateway"), self.url)
        pending = self.runtime.pending[self.uri]
        self.backend.register_binding.assert_called_once_with(self.alice, self.uri, pending.state)
        self.backend.token.return_value = {"sessionStatus": "IN_PROGRESS", "sessionUri": self.uri}
        self.assertEqual(self.runtime.connect(self.alice, "gateway"), self.url)
        self.assertEqual(self.runtime.execute(self.alice, "gateway", "getAccessibleAtlassianResources", {})["error"], "connection_required")
        self.assertEqual(self.runtime.status(self.alice), {"gateway": "connecting"})
        self.backend.token.return_value = {"accessToken": "private-access"}
        self.assertIsNone(self.runtime.connect(self.alice, "gateway"))
        self.backend.token.assert_called_with(self.alice, self.runtime.providers["gateway"], session=self.uri)
        self.assertIn("result", self.runtime.execute(self.alice, "gateway", "getJiraIssue", {"cloudId": "site", "issueIdOrKey": "TEST-1"}))
        self.assertEqual(self.runtime.execute(self.bob, "gateway", "getAccessibleAtlassianResources", {})["error"], "connection_required")
        self.assertEqual(self.transport.call_count, 1)
        self.backend.complete.assert_not_called()
        self.assertNotIn(b"private-access", (Path(self.tmp.name) / "state.sqlite").read_bytes())

    def test_jira_consent_prompt_returns_fixed_portal_without_disconnecting_gateway(self):
        self.backend.token.return_value = {"accessToken": "private-token"}
        self.runtime.connect(self.alice, "gateway")
        self.transport.side_effect = ToolAuthorizationRequired("private-oauth-url-and-state")
        result = self.runtime.execute(self.alice, "gateway", "getAccessibleAtlassianResources", {})
        self.assertEqual(result["error"], "tool_authorization_required")
        self.assertEqual(result["authorization_portal_url"], settings()["portal_url"])
        self.assertIn("Tool permissions", result["message"])
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(self.runtime.status(self.alice), {"gateway": "connected"})
        self.assertEqual(self.runtime.execute(self.bob, "gateway", "getAccessibleAtlassianResources", {})["error"], "connection_required")
        self.transport.assert_called_once()
        self.transport.side_effect = RuntimeError("private-upstream-error")
        self.assertEqual(self.runtime.execute(self.alice, "gateway", "getAccessibleAtlassianResources", {}),
                         {"error": "service_unavailable"})
        self.transport.side_effect = None
        self.assertIn("result", self.runtime.execute(self.alice, "gateway", "getAccessibleAtlassianResources", {}))

    def test_registration_failure_cannot_open_dispatch_gate(self):
        self.backend.register_binding.side_effect = RuntimeError("private metadata")
        with self.assertRaises(ConnectionRejected) as error:
            self.runtime.connect(self.alice, "gateway")
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(self.runtime.status(self.alice), {"gateway": "disconnected"})
        self.assertEqual(self.runtime.pending, {})

    def test_session_failure_expiration_disconnect_and_model_redaction(self):
        self.runtime.connect(self.alice, "gateway")
        self.backend.token.return_value = {"sessionStatus": "FAILED"}
        with self.assertRaises(ConnectionRejected):
            self.runtime.connect(self.alice, "gateway")
        self.assertEqual(self.runtime.status(self.alice), {"gateway": "disconnected"})
        self.backend.token.return_value = {"authorizationUrl": self.url, "sessionUri": self.uri}
        self.runtime.connect(self.alice, "gateway")
        self.now[0] = 601
        self.assertEqual(self.runtime.status(self.alice), {"gateway": "disconnected"})
        self.backend.token.return_value = {"accessToken": "private-access"}
        self.runtime.connect(self.alice, "gateway")
        self.transport.return_value = {"content": [{"text": "private-access"}]}
        self.assertEqual(self.runtime.execute(self.alice, "gateway", "getAccessibleAtlassianResources", {}), {"error": "unsafe_result"})
        self.backend.token.return_value = {"authorizationUrl": "private-url"}
        result = self.runtime.execute(self.alice, "gateway", "getAccessibleAtlassianResources", {})
        self.assertEqual(result["error"], "connection_required")
        self.assertNotIn("private-url", json.dumps(result))
        self.assertEqual(self.runtime.status(self.alice), {"gateway": "disconnected"})
        with self.assertRaises(ConnectionRejected):
            self.runtime.callback("session", "state")
        with self.assertRaises(ConnectionRejected):
            self.runtime.confirm(self.alice, "gateway", "code")


class HostTests(unittest.TestCase):
    def test_mode_uses_agent_role_and_never_starts_a_callback_server(self):
        from agentcore_core import host, vault_gateway, callback
        from unittest.mock import AsyncMock
        fake_gateway = types.ModuleType("gateway")
        fake_gateway.session_context = types.SimpleNamespace(_VAR_MAP={k: object() for k in (
            "HERMES_SESSION_PLATFORM", "HERMES_SESSION_SCOPE_ID", "HERMES_SESSION_USER_ID", "HERMES_CRON_SESSION")})
        ctx, sdk, sts, session, http = Mock(), Mock(), Mock(), Mock(), Mock()
        sts.assume_role.return_value = {"Credentials": {"AccessKeyId": "key", "SecretAccessKey": "secret", "SessionToken": "session"}}
        session.client.return_value = sdk
        sdk.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "workload"}
        sdk.get_resource_oauth2_token.return_value = {"accessToken": "access"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"gateway": fake_gateway}), \
                patch("boto3.client", return_value=sts), patch("boto3.Session", return_value=session), \
                patch("httpx.Client", return_value=http), patch.object(host, "task_identity", return_value=SlackIdentity("TTEST", "UALICE")), \
                patch.object(callback, "start_callback") as callback_server, patch.object(vault_gateway.atexit, "register") as close, \
                patch("agentcore_core.transport.call_mcp", return_value={"content": []}) as mcp:
            raw = settings()
            raw["data_dir"] = directory
            config = Path(directory) / "vault.toml"
            lines = [f"{k} = {json.dumps(v)}" for k, v in raw.items() if not isinstance(v, dict)]
            lines += ["[custom_parameters]", 'identity_provider = "Google"']
            config.write_text("\n".join(lines))
            runtime = host.enable(ctx, config)
            try:
                app = Mock()
                ctx.register_platform_handler.call_args.args[1](app, None)
                ui = app.use.call_args.args[0]
                client = Mock(views_publish=AsyncMock())
                asyncio.run(ui.home(client, SlackIdentity("TTEST", "UALICE")))
                self.assertNotIn("hac:code", json.dumps(client.views_publish.call_args.kwargs))
                runtime.connect(SlackIdentity("TTEST", "UALICE"), "gateway")
                result = json.loads(ctx.register_tool.call_args.kwargs["handler"]({"tool": "getJiraIssue", "arguments": {"cloudId": "site", "issueIdOrKey": "TEST-1"}}))
                self.assertIn("result", result)
                self.assertEqual(mcp.call_args.args[2], "JiraRovoTarget___getJiraIssue")
                self.assertEqual(sts.assume_role.call_args.kwargs["RoleArn"], raw["agent_role_arn"])
                callback_server.assert_not_called()
                sdk.complete_resource_token_auth.assert_not_called()
                with self.assertRaises(ValueError):
                    ui.identity({"team_id": "TTEST", "user": "UOTHER"}, {"team_id": "TTEST", "authorize_result": True})
            finally:
                close.call_args.args[0]()
