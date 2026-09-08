from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import tomllib
import unittest
from unittest.mock import Mock

from agentcore_core.connections import SlackIdentity
from agentcore_core.portal import Login, PortalRuntime, validate_settings


class RequesterDispatchTests(unittest.TestCase):
    def setUp(self):
        with open(Path(__file__).parents[1] / "examples/portal.toml", "rb") as f:
            settings = validate_settings(tomllib.load(f))
        self.now = 1000
        self.transport = Mock()
        self.backend = Mock()
        self.runtime = PortalRuntime(settings, self.backend, self.transport, lambda: self.now)
        self.a = SlackIdentity("TEXAMPLE", "U1")
        self.b = SlackIdentity("TEXAMPLE", "U2")
        self.runtime.logins[self.a.subject] = Login("a", "a", "private-a", 2000)
        self.runtime.logins[self.b.subject] = Login("b", "b", "private-b", 2000)

    def call(self, identity):
        return self.runtime.execute(identity, "getAccessibleAtlassianResources", {})

    def test_overlapping_users_keep_tokens_separate_and_signout_is_prompt(self):
        entered, release = threading.Event(), threading.Event()
        def transport(provider, token, tool, args):
            if token == "private-a":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError()
                return {"content": [{"text": "A data must be discarded"}]}
            self.assertEqual(token, "private-b")
            return {"content": [{"text": "B data"}]}
        self.transport.side_effect = transport
        with ThreadPoolExecutor(max_workers=3) as pool:
            a = pool.submit(self.call, self.a)
            try:
                self.assertTrue(entered.wait(1))
                b = pool.submit(self.call, self.b).result(timeout=1)
                self.assertIn("B data", json.dumps(b))
                pool.submit(self.runtime.signout, self.a).result(timeout=1)
                self.assertEqual(self.call(self.a)["error"], "sign_in_required")
            finally:
                release.set()
            self.assertNotIn("A data", json.dumps(a.result(timeout=1)))
        self.assertEqual(self.runtime.active_requests, {})
        self.backend.authorize.assert_not_called()

    def test_relogin_expiry_and_close_discard_old_results(self):
        for change in ("relogin", "expire", "close"):
            with self.subTest(change=change):
                self.now = 1000
                self.runtime.logins[self.a.subject] = Login("a", "a", "private-a", 2000)
                def transport(*args):
                    if change == "relogin":
                        self.runtime.logins[self.a.subject] = Login("new", "new", "private-new", 2000)
                    elif change == "expire":
                        self.now = 2001
                    else:
                        self.runtime.close()
                    return {"content": [{"text": "old account data"}]}
                self.transport.side_effect = transport
                result = self.call(self.a)
                self.assertEqual(result["error"], "sign_in_required")
                self.assertNotIn("old account", json.dumps(result))
                self.assertEqual(self.runtime.active_requests, {})

    def test_per_requester_limit_does_not_block_other_user(self):
        barrier = threading.Barrier(3)
        release = threading.Event()
        def transport(provider, token, *args):
            if token == "private-a":
                barrier.wait(timeout=2)
                if not release.wait(3):
                    raise TimeoutError()
            return {"content": [{"text": "ok"}]}
        self.transport.side_effect = transport
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.call, self.a) for _ in range(2)]
            try:
                barrier.wait(timeout=2)
                self.assertEqual(self.call(self.a)["error"], "busy")
                self.assertIn("result", self.call(self.b))
            finally:
                release.set()
            for future in futures:
                self.assertIn("result", future.result(timeout=1))
        self.assertEqual(self.runtime.active_requests, {})

    def test_global_limit_and_failure_release(self):
        self.runtime.active_requests = {f"other-{i}": 2 for i in range(16)}
        self.assertEqual(self.call(self.a)["error"], "busy")
        self.transport.assert_not_called()
        self.runtime.active_requests.clear()
        self.transport.side_effect = RuntimeError("private-a authorizationUrl hidden")
        result = self.call(self.a)
        self.assertNotIn("private-a", json.dumps(result))
        self.assertNotIn("hidden", json.dumps(result))
        self.assertEqual(self.runtime.active_requests, {})

    def test_same_member_in_another_workspace_cannot_borrow_login(self):
        self.runtime.s["workspace_ids"].append("TSECOND")
        other = SlackIdentity("TSECOND", "U1")
        self.assertEqual(self.call(other)["error"], "sign_in_required")
        self.transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
