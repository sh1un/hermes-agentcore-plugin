"""Install and verify plugin dependencies against the pinned Hermes project."""
import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def active(requirement):
    return requirement.marker is None or requirement.marker.evaluate()


def check_shared_pins(host, plugin):
    host_requirements = list(host["project"]["dependencies"])
    for requirements in host["project"].get("optional-dependencies", {}).values():
        host_requirements.extend(requirements)
    for text in plugin["project"]["dependencies"]:
        dependency = Requirement(text)
        if not active(dependency):
            continue
        pins = [pin.version for pin in dependency.specifier if pin.operator == "==" and "*" not in pin.version]
        if len(pins) != 1:
            raise ValueError(f"Plugin dependency must have one exact pin: {dependency.name}")
        for host_text in host_requirements:
            requirement = Requirement(host_text)
            if (active(requirement) and canonicalize_name(requirement.name) == canonicalize_name(dependency.name)
                    and pins[0] not in requirement.specifier):
                raise ValueError(f"Plugin pin {dependency.name}=={pins[0]} conflicts with Hermes {requirement.specifier}")


def check_installed(requirements):
    for text in requirements:
        requirement = Requirement(text)
        if active(requirement) and version(requirement.name) not in requirement.specifier:
            raise ValueError(f"Installed {requirement.name} does not match its declared requirement")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Install during image build using Hermes lock overrides")
    args = parser.parse_args()
    root = Path("/opt/hermes")
    plugin_root = root / "plugins/agentcore"
    host = tomllib.loads((root / "pyproject.toml").read_text())
    source_project = plugin_root / "pyproject.toml"
    build_project = plugin_root / "pyproject.build.toml"
    plugin = tomllib.loads((source_project if source_project.is_file() else build_project).read_text())
    requirements = host["project"]["dependencies"] + plugin["project"]["dependencies"]
    check_shared_pins(host, plugin)
    # Respect the host's deliberate overrides (including Pillow), rather than
    # downgrading shared packages to make a standalone pip check appear clean.
    from pm.environment import managed_environment

    environment = managed_environment(root / ".venv", python=Path(sys.executable), explicit=True, output=sys.stderr)
    if args.install:
        environment.install_requirements(requirements, lock=root / "uv.lock")
    check_installed(requirements)
    environment.check(root / "uv.lock")
    if args.install and source_project.is_file():
        # This immutable image ships a ready-to-load directory plugin, not a
        # source workspace that Hermes should resolve/install again at startup.
        # Keep the reviewed metadata for consumer-image checks and provenance.
        source_project.rename(build_project)

    from hermes_cli.plugins import PluginContext
    from gateway import session_context

    for name in ("register_platform_handler", "register_tool", "get_config", "register_cli_command"):
        if not callable(getattr(PluginContext, name, None)):
            raise ValueError("Incompatible Hermes plugin API: " + name)
    for key in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_SCOPE_ID", "HERMES_SESSION_USER_ID", "HERMES_CRON_SESSION"):
        if key not in session_context._VAR_MAP:
            raise ValueError("Incompatible task-local identity API")

    with tempfile.TemporaryDirectory(prefix="agentcore-host-check-") as directory:
        Path(directory, "config.yaml").write_text(json.dumps({"plugins": {"enabled": ["agentcore"]}}))
        result = subprocess.run(
            [str(root / ".venv/bin/hermes"), "agentcore", "doctor", "--discovery-only"],
            env=dict(os.environ, HERMES_HOME=directory, HOME=directory),
            capture_output=True, text=True, timeout=60,
        )
        if (result.returncode != 0 or "AgentCore plugin:" not in result.stdout or
                "out of sync" in result.stdout + result.stderr):
            raise ValueError("Installed AgentCore plugin failed native discovery or managed-environment validation")
    print("Host project pins, installed dependencies and native plugin discovery verified.")


if __name__ == "__main__":
    main()
