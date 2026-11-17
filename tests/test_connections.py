import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from agentcore_core.connections import Store, SlackIdentity, ConnectionRejected


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "connections.sqlite"
        self.store = Store(self.path, "test", frozenset({"jira"}))
        self.addCleanup(lambda: self.store.close())
        self.addCleanup(self.tmp.cleanup)
        self.a = SlackIdentity("T123", "U123")
        self.b = SlackIdentity("T123", "U456")

    def connect(self, person):
        generation = self.store.begin(person, "jira").generation
        self.store.complete_verified(person, "jira", generation)
        return generation

    def test_second_owner_cannot_invalidate_pending_session(self):
        generation = self.store.begin(self.a, "jira").generation
        with self.assertRaises(RuntimeError):
            Store(self.path, "test", frozenset({"jira"}))
        self.store.complete_verified(self.a, "jira", generation)
        self.assertEqual(self.store.status(self.a, "jira").state, "connected")

    def test_identity_isolation_and_disconnect(self):
        ga, gb = self.connect(self.a), self.connect(self.b)
        self.store.disconnect(self.a, "jira")
        operation = Mock()
        with self.assertRaises(ConnectionRejected):
            self.store.dispatch(self.a, "jira", ga, operation)
        operation.assert_not_called()
        self.assertEqual(self.store.dispatch(self.b, "jira", gb, lambda: "ok"), "ok")
        self.assertEqual(self.store.status(SlackIdentity("T999", "U123"), "jira").state,
                         "disconnected")

    def test_old_callbacks_and_replay_rejected(self):
        old = self.store.begin(self.a, "jira").generation
        new = self.store.begin(self.a, "jira").generation
        with self.assertRaises(ConnectionRejected):
            self.store.complete_verified(self.a, "jira", old)
        with self.assertRaises(ConnectionRejected):
            self.store.complete_verified(self.b, "jira", new)
        self.store.complete_verified(self.a, "jira", new)
        with self.assertRaises(ConnectionRejected):
            self.store.complete_verified(self.a, "jira", new)

    def test_disconnect_invalidates_pending(self):
        generation = self.store.begin(self.a, "jira").generation
        self.store.disconnect(self.a, "jira")
        with self.assertRaises(ConnectionRejected):
            self.store.complete_verified(self.a, "jira", generation)

    def test_restart_invalidates_pending_and_preserves_gate(self):
        generation = self.store.begin(self.a, "jira").generation
        self.connect(self.b)
        self.store.disconnect(self.b, "jira")
        self.store.close()
        self.store = Store(self.path, "test", frozenset({"jira"}))
        with self.assertRaises(ConnectionRejected):
            self.store.complete_verified(self.a, "jira", generation)
        self.assertEqual(self.store.status(self.b, "jira").state, "disconnected")

    def test_reject_unknown_connection_and_missing_identity(self):
        for identity, name in [(self.a, "arbitrary"), (None, "jira")]:
            with self.assertRaises(ConnectionRejected):
                self.store.begin(identity, name)

    def test_slack_id_validation(self):
        with self.assertRaises(ValueError):
            SlackIdentity("slack:T123:U456", "U123")

    def test_plugin_registers_only_diagnostic_cli(self):
        spec = importlib.util.spec_from_file_location("plugin", Path(__file__).parents[1] / "__init__.py")
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        host = Mock(spec=["register_cli_command", "get_config"])
        host.get_config.return_value = None
        plugin.register(host)
        host.register_cli_command.assert_called_once()
        self.assertEqual(host.register_cli_command.call_args.kwargs["name"], "agentcore")


if __name__ == "__main__":
    unittest.main()


class LazyStoreTests(unittest.TestCase):
    def test_discovery_and_unused_shutdown_do_not_open_database(self):
        from agentcore_core.connections import LazyStore
        factory = Mock()
        store = LazyStore(factory)
        factory.assert_not_called()
        store.close()
        factory.assert_not_called()
        with self.assertRaises(RuntimeError):
            store.status(SlackIdentity("T123", "U123"), "jira")
        factory.assert_not_called()

    def test_second_process_discovery_does_not_break_active_owner(self):
        from agentcore_core.connections import LazyStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            owner = Store(path, "test", frozenset({"jira"}))
            factory = Mock(side_effect=lambda: Store(path, "test", frozenset({"jira"})))
            observer = LazyStore(factory)
            person = SlackIdentity("T123", "U123")
            try:
                # A doctor/catalog check must not take ownership or reset sessions.
                generation = owner.begin(person, "jira").generation
                factory.assert_not_called()
                with self.assertRaisesRegex(RuntimeError, "already has an owner"):
                    observer.status(person, "jira")
                owner.complete_verified(person, "jira", generation)
                self.assertEqual(owner.status(person, "jira").state, "connected")
            finally:
                observer.close()
                owner.close()

    def test_concurrent_first_use_initializes_once_and_cleanup_is_idempotent(self):
        from agentcore_core.connections import LazyStore
        from concurrent.futures import ThreadPoolExecutor
        backend = Mock()
        backend.status.return_value = "connected"
        factory = Mock(return_value=backend)
        store = LazyStore(factory)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: store.status("person", "jira"), range(12)))
        self.assertEqual(results, ["connected"] * 12)
        factory.assert_called_once()
        store.close()
        store.close()
        backend.close.assert_called_once()
