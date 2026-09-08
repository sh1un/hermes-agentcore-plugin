"""Version-gated Hermes adapter. Never resolve identity from process env."""
import atexit
from contextvars import copy_context
import json
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from .connections import Store, SlackIdentity
from .runtime import AgentCore, Provider, Runtime
from .transport import call_mcp


def task_identity(workspaces):
    from gateway import session_context as sc
    # Read ContextVar values directly. get_session_env falls back to os.environ.
    context = copy_context()
    def value(key):
        var = sc._VAR_MAP.get(key)
        return context.get(var) if var is not None else None
    if value("HERMES_SESSION_PLATFORM") != "slack" or value("HERMES_CRON_SESSION") == "1":
        raise ValueError("Interactive Slack identity required")
    team, user = value("HERMES_SESSION_SCOPE_ID"), value("HERMES_SESSION_USER_ID")
    if team not in workspaces:
        raise ValueError("Workspace not allowed")
    return SlackIdentity(team, user)


def load_settings(path):
    with open(path, "rb") as f:
        settings = tomllib.load(f)
    for key in ("region", "workload", "return_url", "data_dir", "workspace_ids", "providers"):
        if not settings.get(key):
            raise ValueError(f"Missing setting: {key}")
    url = urlsplit(settings["return_url"])
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path != "/oauth/agentcore/callback"):
        raise ValueError("Invalid return URL")
    if not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d", settings["region"]):
        raise ValueError("Invalid region")
    for team in settings["workspace_ids"]:
        SlackIdentity(team, "U0")
    providers = []
    for name, p in settings["providers"].items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", name):
            raise ValueError("Invalid connection name")
        # PoC is explicitly direct Rovo MCP. Gateway has a different auth contract.
        if p["endpoint"] != "https://mcp.atlassian.com/v1/mcp/authv2":
            raise ValueError("Only the verified direct Atlassian endpoint is supported")
        if not p.get("scopes") or not p.get("credential_provider"):
            raise ValueError("Provider scopes and credential provider are required")
        providers.append(Provider(name, p["credential_provider"], p["endpoint"],
                                  tuple(p["scopes"]), tuple(p.get("resources", []))))
    if len(providers) > 8:
        raise ValueError("At most eight connections are supported")
    return settings, providers


def enable(ctx, config_file):
    if not callable(getattr(ctx, "register_platform_handler", None)):
        raise RuntimeError("Hermes register_platform_handler is required")
    from gateway import session_context as sc
    for key in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_SCOPE_ID", "HERMES_SESSION_USER_ID", "HERMES_CRON_SESSION"):
        if key not in getattr(sc, "_VAR_MAP", {}):
            raise RuntimeError("Hermes task-local identity API is incompatible")
    import boto3
    from botocore.config import Config
    from .slack_ui import NativeConnections
    from .callback import start_callback
    settings, providers = load_settings(config_file)
    store = Store(Path(settings["data_dir"]).expanduser() / "connections.sqlite",
                  settings["region"] + ":" + settings["workload"], frozenset(p.name for p in providers))
    client = boto3.client("bedrock-agentcore", region_name=settings["region"],
                         config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 1}))
    runtime = Runtime(store, AgentCore(client, settings["region"], settings["workload"],
                                     settings["return_url"]), call_mcp, providers)
    servers = []
    def wire(app, adapter):
        if servers:
            raise RuntimeError("Only one Slack adapter per plugin instance is supported")
        server = start_callback(runtime, settings.get("callback_port", 8849))
        servers.append(server)
        app.use(NativeConnections(runtime, settings["workspace_ids"]))

    def shutdown():
        for server in servers:
            server.shutdown()
            server.server_close()
        store.close()
    atexit.register(shutdown)
    ctx.register_platform_handler("slack", wire)

    def execute(args, **kwargs):
        try:
            if not isinstance(args, dict) or set(args) != {"connection", "tool", "arguments"}:
                return json.dumps({"error": "invalid_arguments"})
            identity = task_identity(settings["workspace_ids"])
            return json.dumps(runtime.execute(identity, args["connection"], args["tool"], args["arguments"]))
        except Exception:
            return json.dumps({"error": "identity_unavailable"})

    ctx.register_tool(name="agentcore_jira_read", toolset="agentcore", handler=execute,
        schema={"name": "agentcore_jira_read", "description": "Read Jira using the current Slack user's connection. Manage connections in the app Home.",
            "parameters": {"type": "object", "additionalProperties": False,
                "required": ["connection", "tool", "arguments"], "properties": {
                    "connection": {"type": "string", "enum": list(runtime.providers)},
                    "tool": {"type": "string", "enum": ["getAccessibleAtlassianResources", "getJiraIssue"]},
                    "arguments": {"type": "object", "description": "Empty for resources, or cloudId and issueIdOrKey for an issue"}}}})
    return runtime
