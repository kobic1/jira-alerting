"""Shared data models for the alerting pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1}[self.value]


# group_by values understood by the engine
GROUP_BY_ASSIGNEE = "assignee"
GROUP_BY_REPORTER = "reporter"
GROUP_BY_NOTIFY_ROLE = "notify_role"   # fan-out: one digest per person holding notify_roles
GROUP_BY_PROJECT_LEAD = "project_lead" # always route to the issue's project lead


@dataclass
class JiraUser:
    account_id: str
    display_name: str
    email: str | None = None

    @classmethod
    def from_api(cls, data: dict) -> JiraUser:
        return cls(
            account_id=data.get("accountId", ""),
            display_name=data.get("displayName", "Unknown"),
            email=data.get("emailAddress"),
        )


# SLA definitions: (priority, regression) -> days to resolve
# priority is the Jira priority name; regression is "Yes"/"No"/None
_SLA_DAYS: dict[tuple[str, str | None], int] = {
    ("P1",  None):  3,
    ("P1",  "Yes"): 3,
    ("P1",  "No"):  3,
    ("P2",  "Yes"): 7,
    ("P3",  "Yes"): 7,
    ("P4",  "Yes"): 7,
    ("P2",  "No"):  14,
    ("P2",  None):  14,   # treat unknown regression same as No for P2
    ("P3",  "No"):  30,
    ("P3",  None):  30,
    ("P4",  "No"):  30,
    ("P4",  None):  30,
}
_SLA_WARNING_DAYS = 1   # alert when this many days remain before breach


@dataclass
class JiraIssue:
    key: str
    summary: str
    issue_type: str
    status: str
    priority: str | None
    assignee: JiraUser | None
    reporter: JiraUser | None
    created_at: datetime
    updated_at: datetime
    base_url: str
    labels: list[str] = field(default_factory=list)
    regression: str | None = None   # "Yes" / "No" / None  (customfield_10055)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def url(self) -> str:
        return f"{self.base_url}/browse/{self.key}"

    @property
    def age_days(self) -> int:
        return (datetime.utcnow() - self.created_at).days

    @property
    def days_since_update(self) -> int:
        return (datetime.utcnow() - self.updated_at).days

    @property
    def cycle_time_days(self) -> int:
        """Days since first transition to In Progress (approximated by created_at)."""
        in_progress_date = self.raw.get("_in_progress_date")
        if in_progress_date:
            return (datetime.utcnow() - in_progress_date).days
        return self.age_days

    @property
    def sla_days(self) -> int | None:
        """SLA target in days based on priority + regression. None if unknown priority."""
        p = self.priority
        r = self.regression
        if not p:
            return None
        return _SLA_DAYS.get((p, r)) or _SLA_DAYS.get((p, None))

    @property
    def sla_remaining_days(self) -> int | None:
        """Days remaining before SLA breach (negative = already breached)."""
        sla = self.sla_days
        if sla is None:
            return None
        return sla - self.age_days

    @property
    def sla_pct_used(self) -> float | None:
        """Fraction of SLA consumed (0.0–1.0+). None if SLA unknown."""
        sla = self.sla_days
        if not sla:
            return None
        return self.age_days / sla

    @property
    def sla_status(self) -> str:
        """'breached', 'warning' (within 1 day), or 'ok'."""
        remaining = self.sla_remaining_days
        if remaining is None:
            return "ok"
        if remaining < 0:
            return "breached"
        if remaining <= _SLA_WARNING_DAYS:
            return "warning"
        return "ok"

    @classmethod
    def from_api(cls, data: dict, base_url: str) -> JiraIssue:
        fields = data["fields"]
        assignee_data = fields.get("assignee")
        reporter_data = fields.get("reporter")
        regression_raw = fields.get("customfield_10055")
        regression = regression_raw.get("value") if isinstance(regression_raw, dict) else None
        return cls(
            key=data["key"],
            summary=fields.get("summary", ""),
            issue_type=fields.get("issuetype", {}).get("name", ""),
            status=fields.get("status", {}).get("name", ""),
            priority=(fields.get("priority") or {}).get("name"),
            assignee=JiraUser.from_api(assignee_data) if assignee_data else None,
            reporter=JiraUser.from_api(reporter_data) if reporter_data else None,
            created_at=_parse_dt(fields.get("created")),
            updated_at=_parse_dt(fields.get("updated")),
            labels=fields.get("labels", []),
            regression=regression,
            base_url=base_url,
            raw=data,
        )


@dataclass
class RuleConfig:
    id: str
    name: str
    description: str
    jql: str
    conditions: list[dict]
    group_by: str          # assignee | reporter | notify_role
    severity: Severity
    message_template: str
    # status: Disabled | POC | Live
    #   Disabled → rule never runs
    #   POC      → runs in preview mode only (messages go to preview reviewer)
    #   Live     → runs normally, messages sent to real recipients
    status: str = "Live"
    jira_filter_id: str | None = None

    @property
    def enabled(self) -> bool:
        return self.status.lower() != "disabled"
    # When group_by == "notify_role": the digest is sent to every person whose
    # role appears in this list, regardless of who the Jira assignee is.
    notify_roles: list[str] = field(default_factory=list)
    # Extra recipients who always get a copy of this rule's matches, on top of
    # whoever the primary group_by routes to. Useful for managers who want a heads-up
    # on a specific rule without being the primary owner.
    also_notify_roles: list[str] = field(default_factory=list)
    # When group_by == "assignee" and the issue has no assignee, route to the
    # first person in people.yaml who holds this role (e.g. "engineering_manager").
    fallback_assignee_role: str | None = None
    # Display order in the digest (lower = appears first). Default 100.
    priority: int = 100
    # Optional color-dot override (e.g. "🟠"). Falls back to the severity emoji.
    emoji: str | None = None


@dataclass
class RuleMatch:
    rule: RuleConfig
    issue: JiraIssue
    context: dict[str, Any] = field(default_factory=dict)
    # Set when the rule uses notify_role grouping — the specific person
    # this match is addressed to (may differ from the Jira assignee).
    notified_person_key: str | None = None
    # Pre-resolved owner for notify_role rules (carries display_name / email).
    owner_override: "JiraUser | None" = None

    @property
    def owner_key(self) -> str:
        if self.notified_person_key:
            return self.notified_person_key
        if self.rule.group_by == GROUP_BY_ASSIGNEE:
            return self.issue.assignee.account_id if self.issue.assignee else "unassigned"
        if self.rule.group_by == GROUP_BY_REPORTER:
            return self.issue.reporter.account_id if self.issue.reporter else "unassigned"
        return "unassigned"

    @property
    def owner(self) -> JiraUser | None:
        if self.owner_override:
            return self.owner_override
        if self.rule.group_by == GROUP_BY_ASSIGNEE:
            return self.issue.assignee
        if self.rule.group_by == GROUP_BY_REPORTER:
            return self.issue.reporter
        return None


@dataclass
class AlertGroup:
    owner: JiraUser | None
    owner_key: str
    matches: list[RuleMatch] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.owner.display_name if self.owner else "Unassigned"


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.utcnow()
    try:
        # Normalize: Z -> +00:00, and +0300 -> +03:00 (Python 3.9 fromisoformat
        # requires the colon in the UTC offset; Jira sometimes omits it)
        import re as _re
        v = value.replace("Z", "+00:00")
        v = _re.sub(r"([+-])(\d{2})(\d{2})$", r"\1\2:\3", v)
        return datetime.fromisoformat(v).replace(tzinfo=None)
    except ValueError:
        return datetime.utcnow()
