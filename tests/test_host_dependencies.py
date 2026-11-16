"""Reject plugin pins that a host project cannot resolve before publishing an image."""
import importlib.util
from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_host", ROOT / ".ci/hermes-plugin/check-host.py")
check_host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_host)


class HostDependencyTests(unittest.TestCase):
    def test_old_pyjwt_pin_is_rejected_even_if_standalone_dependencies_install(self):
        host = {"project": {"dependencies": ["PyJWT[crypto]==2.15.1"]}}
        plugin = {"project": {"dependencies": ["PyJWT[crypto]==2.13.0"]}}
        with self.assertRaisesRegex(ValueError, "conflicts with Hermes"):
            check_host.check_shared_pins(host, plugin)

    def test_conflict_with_a_host_extra_is_also_rejected(self):
        host = {"project": {"dependencies": [], "optional-dependencies": {"mcp": ["mcp==2.3.0"]}}}
        plugin = {"project": {"dependencies": ["mcp==2.0.0"]}}
        with self.assertRaisesRegex(ValueError, "conflicts with Hermes"):
            check_host.check_shared_pins(host, plugin)

    def test_matching_pin_and_irrelevant_platform_requirement(self):
        host = {"project": {"dependencies": ["PyJWT[crypto]==2.15.1", "PyJWT==1.0.0; sys_platform == 'never'"]}}
        plugin = {"project": {"dependencies": ["PyJWT[crypto]==2.15.1"]}}
        check_host.check_shared_pins(host, plugin)

    def test_requirements_and_project_metadata_use_the_same_reviewed_pins(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        requirements = [line for line in (ROOT / "requirements.txt").read_text().splitlines() if line and not line.startswith("#")]
        self.assertEqual(requirements, project["project"]["dependencies"])
