"""Local tool visibility diagnostics. Never fetch tokens or print settings values."""
import json


TOOL = "agentcore_jira_read"


def report(config_file, config, registry, select_toolsets, definitions):
    result = {
        "configuration": "configured" if config_file else "missing",
        "registered": registry.get_entry(TOOL) is not None,
        "slack_toolset_enabled": False,
        "slack_catalog_contains_tool": False,
        "scope": "current_process_and_profile_only",
    }
    if not config_file:
        return result
    selected = sorted(select_toolsets(config, "slack"))
    result["slack_toolset_enabled"] = "agentcore" in selected
    schemas = definitions(enabled_toolsets=selected, quiet_mode=True, skip_tool_search_assembly=True)
    result["slack_catalog_contains_tool"] = any(
        schema.get("function", {}).get("name") == TOOL for schema in schemas
    )
    return result


def run(ctx, args):
    print("AgentCore plugin: local tool visibility check (no AWS requests).")
    if getattr(args, "discovery_only", False):
        print("Directory plugin discovered. Runtime configuration and tools were not checked.")
        return
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.tools_config import _get_platform_tools
        from tools.registry import registry
        from model_tools import get_tool_definitions

        result = report(ctx.get_config("config_file"), load_config_readonly(), registry,
                        _get_platform_tools, get_tool_definitions)
    except Exception:
        # Host/parser errors can include config values, so do not print exceptions.
        print("AgentCore diagnostics failed. Check plugin startup logs without sharing credentials.")
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))
    if not (result["configuration"] == "configured" and result["registered"]
            and result["slack_toolset_enabled"] and result["slack_catalog_contains_tool"]):
        print("Tool unavailable in this profile. Check the AgentCore config and Slack toolset selection.")
        raise SystemExit(1)
    print("Tool available in this process. A running chat can still have different profile or session settings.")
