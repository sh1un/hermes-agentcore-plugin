import asyncio
from contextvars import ContextVar
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import boto3
from botocore.stub import Stubber
from slack_bolt.request.async_request import AsyncBoltRequest
from slack_bolt.response import BoltResponse

from agentcore_core.connections import SlackIdentity
from agentcore_core.runtime import AgentCore, Provider
from agentcore_core.slack_ui import NativeConnections
from agentcore_core.host import task_identity, load_settings


class AWSTests(unittest.TestCase):
    def test_sdk_shapes_and_subject(self):
        client = boto3.client("bedrock-agentcore", region_name="us-east-1",
                              aws_access_key_id="testing", aws_secret_access_key="testing")
        backend = AgentCore(client, "us-east-1", "example", "https://example.com/oauth/agentcore/callback")
        person = SlackIdentity("T123", "U123")
        provider = Provider("jira", "example", "https://example.com", ("read",))
        with Stubber(client) as stub:
            stub.add_response("get_workload_access_token_for_user_id", {"workloadAccessToken": "workload"},
                              {"workloadName": "example", "userId": person.subject})
            stub.add_response("get_resource_oauth2_token", {"accessToken": "token"},
                {"workloadIdentityToken": "workload", "resourceCredentialProviderName": "example",
                 "scopes": ["read"], "oauth2Flow": "USER_FEDERATION",
                 "resourceOauth2ReturnUrl": backend.return_url, "customState": "state"})
            self.assertEqual(backend.token(person, provider, "state")["accessToken"], "token")
            stub.add_response("complete_resource_token_auth", {},
                              {"userIdentifier": {"userId": person.subject}, "sessionUri": "urn:session"})
            backend.complete(person, "urn:session")
            stub.assert_no_pending_responses()

    def test_auth_url_allowlist(self):
        backend = AgentCore(None, "us-east-1", "example", "https://example.com")
        backend.validate_url("https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/authorize?x=1")
        for url in ["https://evil.example/identities/oauth2/authorize", "http://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/authorize"]:
            with self.assertRaises(ValueError):
                backend.validate_url(url)


class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_bolt_pipeline_consumes_home_before_catchall(self):
        import re
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.authorization.authorize_result import AuthorizeResult

        async def authorize(**kwargs):
            return AuthorizeResult(enterprise_id=None, team_id="T123", bot_token="test", bot_id="B123", bot_user_id="U999")

        app = AsyncApp(authorize=authorize, request_verification_enabled=False)
        listener = AsyncMock()
        @app.event(re.compile(".*"))
        async def catchall(event):
            await listener(event)
        runtime = Mock()
        runtime.status.return_value = {"jira": "disconnected"}
        ui = NativeConnections(runtime, ["T123"])
        ui.home = AsyncMock()
        app.use(ui)
        req = AsyncBoltRequest(body={"type": "event_callback", "team_id": "T123",
            "event": {"type": "app_home_opened", "user": "U123"}}, mode="socket_mode")
        response = await app.async_dispatch(req)
        self.assertEqual(response.status, 200)
        await asyncio.gather(*list(ui.tasks))
        ui.home.assert_awaited_once()
        listener.assert_not_awaited()

    async def test_pasted_authorization_artifacts_are_not_forwarded(self):
        ui = NativeConnections(Mock(), ["T123"])
        next_listener = AsyncMock()
        for text in ["HAC-example", "https://example.com/identities/oauth2/authorize?state=secret"]:
            req = AsyncBoltRequest(body={"event": {"type": "message", "text": text}}, mode="socket_mode")
            response = await ui.async_process(req=req, resp=BoltResponse(status=200), next=next_listener)
            self.assertEqual(response.status, 200)
        next_listener.assert_not_awaited()

    async def test_native_events_never_reach_model_listener(self):
        runtime = Mock()
        runtime.providers = {"jira": None}
        runtime.status.return_value = {"jira": "connected"}
        ui = NativeConnections(runtime, ["T123"])
        client = Mock()
        client.views_publish = AsyncMock()
        client.views_open = AsyncMock()
        bodies = [
            {"type": "event_callback", "team_id": "T123", "event": {"type": "app_home_opened", "user": "U123"}},
            {"type": "block_actions", "team": {"id": "T123"}, "user": {"id": "U123"},
             "actions": [{"action_id": "hac:disconnect", "value": "jira"}]},
            {"type": "view_submission", "team": {"id": "T123"}, "user": {"id": "U123"},
             "view": {"callback_id": "hac:confirm", "private_metadata": "jira",
                      "state": {"values": {"code": {"code": {"value": "HAC-test"}}}}}},
        ]
        next_listener = AsyncMock(side_effect=AssertionError("Model listener reached"))
        for body in bodies:
            req = AsyncBoltRequest(body=body, mode="socket_mode")
            req.context.update(team_id="T123", authorize_result=object(), client=client)
            result = await ui.async_process(req=req, resp=BoltResponse(status=200), next=next_listener)
            self.assertEqual(result.status, 200)
            if ui.tasks:
                await asyncio.gather(*list(ui.tasks))
        next_listener.assert_not_awaited()
        runtime.confirm.assert_called_once_with(SlackIdentity("T123", "U123"), "jira", "HAC-test")

    async def test_ordinary_message_passes_through_and_spoof_fails(self):
        ui = NativeConnections(Mock(), ["T123"])
        next_listener = AsyncMock(return_value=BoltResponse(status=200))
        req = AsyncBoltRequest(body={"event": {"type": "message", "text": "hello"}}, mode="socket_mode")
        await ui.async_process(req=req, resp=BoltResponse(status=200), next=next_listener)
        next_listener.assert_awaited_once()
        req = AsyncBoltRequest(body={"team_id": "T123", "event": {"type": "app_home_opened", "user": "U123"}}, mode="socket_mode")
        req.context.update(team_id="TEVIL", authorize_result=object())
        self.assertEqual((await ui.async_process(req=req, resp=BoltResponse(status=200), next=next_listener)).status, 403)


class HostTests(unittest.TestCase):
    def test_task_context_never_falls_back_to_environment(self):
        keys = {"HERMES_SESSION_PLATFORM": "slack", "HERMES_SESSION_SCOPE_ID": "T123", "HERMES_SESSION_USER_ID": "U123"}
        variables = {k: ContextVar(k) for k in keys}
        module = types.ModuleType("gateway")
        module.session_context = types.SimpleNamespace(_VAR_MAP=variables)
        with patch.dict(sys.modules, {"gateway": module}), patch.dict("os.environ", keys):
            with self.assertRaises(ValueError):
                task_identity(["T123"])
            tokens = [(variables[k], variables[k].set(v)) for k, v in keys.items()]
            try:
                self.assertEqual(task_identity(["T123"]).subject, "slack:T123:U123")
            finally:
                for variable, token in tokens:
                    variable.reset(token)

    def test_example_is_valid(self):
        settings, providers = load_settings(Path(__file__).parents[1] / "examples/agentcore.toml")
        self.assertEqual(len(providers), 1)
