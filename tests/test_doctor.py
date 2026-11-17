"""Exercise unavailable-tool diagnostics without credentials or a Hermes runtime."""
import json
import unittest
from unittest.mock import Mock

from agentcore_core.doctor import report


class DoctorTests(unittest.TestCase):
    def check(self, *, configured=True, registered=True, selected=True, visible=True):
        registry = Mock()
        registry.get_entry.return_value = object() if registered else None
        selector = Mock(return_value=["terminal", "agentcore"] if selected else ["terminal"])
        definitions = Mock(return_value=[{"function": {"name": "agentcore_jira_read"}}] if visible else [])
        result = report("/private/settings.toml" if configured else None,
                        {"secret": "must-not-appear"}, registry, selector, definitions)
        self.assertNotIn("must-not-appear", json.dumps(result))
        self.assertNotIn("/private/settings.toml", json.dumps(result))
        return result, selector, definitions

    def test_missing_configuration_is_not_reported_as_healthy(self):
        result, selector, definitions = self.check(configured=False)
        self.assertEqual(result["configuration"], "missing")
        self.assertFalse(result["slack_catalog_contains_tool"])
        selector.assert_not_called()
        definitions.assert_not_called()

    def test_registered_tool_can_still_be_hidden_from_slack(self):
        result, _, _ = self.check(selected=False, visible=False)
        self.assertTrue(result["registered"])
        self.assertFalse(result["slack_toolset_enabled"])
        self.assertFalse(result["slack_catalog_contains_tool"])

    def test_selected_toolset_does_not_prove_tool_was_registered(self):
        result, _, _ = self.check(registered=False, visible=False)
        self.assertTrue(result["slack_toolset_enabled"])
        self.assertFalse(result["registered"])
        self.assertFalse(result["slack_catalog_contains_tool"])

    def test_healthy_report_checks_actual_model_catalog(self):
        result, selector, definitions = self.check()
        self.assertTrue(result["slack_catalog_contains_tool"])
        self.assertEqual(selector.call_args.args[1], "slack")
        definitions.assert_called_once_with(enabled_toolsets=["agentcore", "terminal"],
                                            quiet_mode=True, skip_tool_search_assembly=True)
        self.assertEqual(result["scope"], "current_process_and_profile_only")
