"""Fail image build if the required native Hermes APIs are missing."""
import inspect
from hermes_cli.plugins import PluginContext
from gateway import session_context

for name in ("register_platform_handler", "register_tool", "get_config"):
    if not callable(getattr(PluginContext, name, None)):
        raise SystemExit("Incompatible Hermes plugin API: " + name)
for key in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_SCOPE_ID", "HERMES_SESSION_USER_ID", "HERMES_CRON_SESSION"):
    if key not in session_context._VAR_MAP:
        raise SystemExit("Incompatible task-local identity API")
print("Hermes native Plugin interfaces found. Live Slack acceptance is still required.")
