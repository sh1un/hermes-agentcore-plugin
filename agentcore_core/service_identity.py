"""Admin-configured routing. Never constructed from model tool arguments."""
from dataclasses import dataclass
import re

from .connections import SlackIdentity


@dataclass(frozen=True)
class ServiceOwner(SlackIdentity):
    service: str

    @property
    def subject(self):
        return f"service:{self.workspace}:{self.service}"


def validate_services(settings):
    services = settings.get("service_accounts", {})
    if not isinstance(services, dict) or len(services) > 4:
        raise ValueError("At most four service accounts are supported")
    routes = set()
    for name, cfg in services.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name)
                or not isinstance(cfg, dict) or set(cfg) != {
                    "workspace_id", "expected_sub", "admin_members", "allowed_members", "channel_ids"}):
            raise ValueError("Invalid service account configuration")
        if cfg["workspace_id"] not in settings["workspace_ids"]:
            raise ValueError("Service workspace is not allowed")
        if not isinstance(cfg["expected_sub"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cfg["expected_sub"]):
            raise ValueError("Pin the service Cognito sub")
        for key in ("admin_members", "allowed_members", "channel_ids"):
            if not isinstance(cfg[key], list) or not cfg[key] or len(cfg[key]) != len(set(cfg[key])):
                raise ValueError("Explicit nonempty service allowlists are required")
        for member in cfg["admin_members"] + cfg["allowed_members"]:
            SlackIdentity(cfg["workspace_id"], member)
        for channel in cfg["channel_ids"]:
            if not isinstance(channel, str) or not re.fullmatch(r"[CG][A-Z0-9]+", channel):
                raise ValueError("Service routes require Slack channel IDs")
            route = cfg["workspace_id"], channel
            if route in routes:
                raise ValueError("Duplicate service channel route")
            routes.add(route)
    return services
