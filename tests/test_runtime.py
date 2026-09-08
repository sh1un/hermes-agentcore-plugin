from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from agentcore_core.connections import Store, SlackIdentity, ConnectionRejected
from agentcore_core.runtime import Runtime, Provider


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "state", "test", frozenset({"jira"}))
        self.addCleanup(self.store.close)
        self.backend = Mock()
        self.transport = Mock(return_value={"content": [{"type": "text", "text": "issue"}]})
        self.now = [0]
        self.runtime = Runtime(self.store, self.backend, self.transport,
            [Provider("jira", "example", "https://example.com", ("read",))], lambda: self.now[0])
        self.a, self.b = SlackIdentity("T123", "U123"), SlackIdentity("T123", "U456")
        self.response = {"authorizationUrl": "https://example.com/secret-url",
                         "sessionUri": "urn:ietf:params:oauth:request_uri:session"}
        self.backend.token.return_value = self.response

    def pending(self):
        url = self.runtime.connect(self.a, "jira")
        self.assertEqual(url, self.response["authorizationUrl"])
        state = self.backend.token.call_args.args[2]
        return self.response["sessionUri"], state

    def test_end_to_end_native_then_read_disconnect(self):
        session, state = self.pending()
        code = self.runtime.callback(session, state)
        self.assertNotEqual(code, state)
        with self.assertRaises(ConnectionRejected):
            self.runtime.confirm(self.b, "jira", code)
        self.runtime.confirm(self.a, "jira", code)
        self.backend.complete.assert_called_once_with(self.a, session)
        with self.assertRaises(ConnectionRejected):
            self.runtime.confirm(self.a, "jira", code)
        self.backend.token.return_value = {"accessToken": "secret-token"}
        result = self.runtime.execute(self.a, "jira", "getJiraIssue", {"cloudId": "site", "issueIdOrKey": "TEST-1"})
        self.assertIn("result", result)
        self.assertNotIn("secret-token", str(result))
        self.assertEqual(self.runtime.disconnect(self.a, "jira")["remote_revocation"], "not_supported")
        self.backend.token.reset_mock()
        self.assertEqual(self.runtime.execute(self.a, "jira", "getAccessibleAtlassianResources", {})["error"], "connection_required")
        self.backend.token.assert_not_called()

    def test_expiration_and_state_replay(self):
        session, state = self.pending()
        with self.assertRaises(ConnectionRejected):
            self.runtime.callback(session, "wrong")
        code = self.runtime.callback(session, state)
        with self.assertRaises(ConnectionRejected):
            self.runtime.callback(session, state)
        self.now[0] = 601
        with self.assertRaises(ConnectionRejected):
            self.runtime.confirm(self.a, "jira", code)
        self.assertEqual(self.runtime.status(self.a)["jira"], "disconnected")

    def test_disconnect_cancels_callback(self):
        session, state = self.pending()
        self.runtime.disconnect(self.a, "jira")
        with self.assertRaises(ConnectionRejected):
            self.runtime.callback(session, state)

    def test_connect_resume_and_status_do_not_start_new_oauth(self):
        self.pending()
        self.runtime.connect(self.a, "jira")
        self.runtime.status(self.a)
        self.assertEqual(self.backend.token.call_count, 1)

    def test_model_never_receives_sdk_auth_or_exception(self):
        self.backend.token.return_value = {"accessToken": "secret-token"}
        self.runtime.connect(self.a, "jira")
        self.backend.token.return_value = self.response
        result = self.runtime.execute(self.a, "jira", "getAccessibleAtlassianResources", {})
        self.assertEqual(result["error"], "connection_required")
        self.assertNotIn("secret-url", str(result))
        self.backend.token.side_effect = RuntimeError("secret-token secret-url")
        result = self.runtime.execute(self.a, "jira", "getAccessibleAtlassianResources", {})
        self.assertEqual(result, {"error": "service_unavailable"})

    def test_write_and_subject_injection_rejected(self):
        for tool, args in [("createJiraIssue", {}), ("getAccessibleAtlassianResources", {"subject": self.b.subject})]:
            self.assertEqual(self.runtime.execute(self.a, "jira", tool, args), {"error": "invalid_arguments"})
        self.backend.token.assert_not_called()

    def test_returned_token_is_filtered(self):
        self.backend.token.return_value = {"accessToken": "secret-token"}
        self.runtime.connect(self.a, "jira")
        self.transport.return_value = {"content": [{"text": "secret-token"}]}
        self.assertEqual(self.runtime.execute(self.a, "jira", "getAccessibleAtlassianResources", {}), {"error": "unsafe_result"})

    def test_database_contains_no_oauth_secrets(self):
        session, state = self.pending()
        code = self.runtime.callback(session, state)
        data = (Path(self.tmp.name) / "state").read_bytes()
        for secret in [session, state, code, "secret-url"]:
            self.assertNotIn(secret.encode(), data)
