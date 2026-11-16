"""Fail the image build when native plugin loading is incompatible."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

from hermes_cli.plugins import PluginContext
from gateway import session_context

for name in ("register_platform_handler", "register_tool", "get_config", "register_cli_command"):
    if not callable(getattr(PluginContext, name, None)):
        raise SystemExit("Incompatible Hermes plugin API: " + name)
for key in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_SCOPE_ID", "HERMES_SESSION_USER_ID", "HERMES_CRON_SESSION"):
    if key not in session_context._VAR_MAP:
        raise SystemExit("Incompatible task-local identity API")

with tempfile.TemporaryDirectory(prefix="agentcore-host-check-") as directory:
    Path(directory, "config.yaml").write_text(json.dumps({"plugins": {"enabled": ["agentcore"]}}))
    result = subprocess.run(
        ["/opt/hermes/.venv/bin/hermes", "agentcore", "doctor"],
        env=dict(os.environ, HERMES_HOME=directory, HOME=directory),
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0 or "AgentCore plugin:" not in result.stdout:
        raise SystemExit("Installed AgentCore plugin failed native Hermes CLI discovery")
print("Hermes discovered the installed AgentCore plugin and ran its doctor command.")
