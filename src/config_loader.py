"""Configuration loader — reads YAML files and resolves environment variables."""
from __future__ import annotations

import os
import re
import yaml

from src.ingestion.people_registry import PeopleRegistry
from src.models import GROUP_BY_NOTIFY_ROLE, RuleConfig, Severity


def _resolve_env(value: str) -> str:
    """Replace ${VAR} placeholders with environment variable values."""
    def replacer(match):
        var = match.group(1)
        resolved = os.environ.get(var)
        if resolved is None:
            raise EnvironmentError(f"Required environment variable not set: {var}")
        return resolved

    return re.sub(r"\$\{([^}]+)\}", replacer, str(value))


def _resolve_dict(d: dict) -> dict:
    result = {}
    for k, v in d.items():
        if isinstance(v, str):
            result[k] = _resolve_env(v)
        elif isinstance(v, dict):
            result[k] = _resolve_dict(v)
        else:
            result[k] = v
    return result


def load_settings(path: str) -> dict:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return _resolve_dict(raw)


def load_rules(path: str) -> list[RuleConfig]:
    with open(path) as f:
        data = yaml.safe_load(f)

    rules = []
    for r in data.get("rules", []):
        notify_roles = r.get("notify_roles", [])
        # Auto-infer group_by when notify_roles is present but group_by is omitted
        group_by = r.get("group_by", GROUP_BY_NOTIFY_ROLE if notify_roles else "assignee")

        # Support both old `enabled: bool` and new `status: disabled|poc|live`.
        # If only `enabled` is present, map it: false→disabled, true→live.
        raw_status = r.get("status")
        if raw_status is None:
            raw_status = "Live" if r.get("enabled", True) else "Disabled"

        rules.append(
            RuleConfig(
                id=r["id"],
                name=r["name"],
                description=r.get("description", ""),
                jql=r["jql"],
                conditions=r.get("conditions", []),
                group_by=group_by,
                severity=Severity(r.get("severity", "medium")),
                message_template=r.get("message_template", ""),
                status=raw_status,
                jira_filter_id=r.get("jira_filter_id"),
                notify_roles=notify_roles,
                also_notify_roles=r.get("also_notify_roles", []),
                fallback_assignee_role=r.get("fallback_assignee_role"),
                priority=int(r.get("priority", 100)),
                emoji=r.get("emoji"),
            )
        )
    return rules


def load_people(path: str) -> PeopleRegistry:
    with open(path) as f:
        data = yaml.safe_load(f)
    return PeopleRegistry.from_yaml(data or {})
