"""Rule engine — evaluates rules against fetched issues and emits AlertGroups.

Two grouping modes
──────────────────
1. group_by: assignee | reporter   (existing behaviour)
   The alert owner is the Jira user in that field.
   e.g. "alert the developer assigned to this aging bug"

2. group_by: notify_role           (new)
   The rule carries notify_roles: [product_manager, …].
   Every matching issue is broadcast to EVERY person who holds one of those
   roles.  Each role-holder gets their own AlertGroup (and therefore their
   own personal digest) containing all matching issues.
   e.g. "all PMs get notified about every epic in Validation"

Role-aware conditions
─────────────────────
When a condition uses operator: days_since_role_comment, the engine:
  1. Calls Jira to fetch the issue's comments (cached per issue per run).
  2. Checks whether the notified person (or any person with the target role
     when no specific person is in scope yet) has commented within <value> days.
  3. The condition PASSES when they have NOT commented recently — meaning the
     alert should fire because feedback is overdue.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from src.ingestion.jira_client import JiraClient, IssueComment
from src.ingestion.people_registry import PeopleRegistry, Person
from src.models import (
    AlertGroup,
    GROUP_BY_ASSIGNEE,
    GROUP_BY_NOTIFY_ROLE,
    GROUP_BY_REPORTER,
    JiraIssue,
    JiraUser,
    RuleConfig,
    RuleMatch,
)
from src.rules.conditions import evaluate_condition, is_role_aware

logger = logging.getLogger(__name__)


def _business_days_between(start: datetime, end: datetime) -> int:
    """Whole business days (Mon–Fri, weekends excluded) elapsed from start to end."""
    if end <= start:
        return 0
    days = 0
    cur = start.date()
    last = end.date()
    while cur < last:
        cur += timedelta(days=1)
        if cur.weekday() < 5:  # Mon=0 … Fri=4, so Sat/Sun are skipped
            days += 1
    return days


# ---------------------------------------------------------------------------
# Field extractors — map condition field names to JiraIssue attributes
# ---------------------------------------------------------------------------

_FIELD_EXTRACTORS: dict[str, callable] = {
    "age_days":            lambda issue: issue.age_days,
    "days_since_update":   lambda issue: issue.days_since_update,
    "cycle_time_days":     lambda issue: issue.cycle_time_days,
    "assignee":            lambda issue: issue.assignee,
    "reporter":            lambda issue: issue.reporter,
    "status":              lambda issue: issue.status,
    "priority":            lambda issue: issue.priority,
    "issue_type":          lambda issue: issue.issue_type,
    "labels":              lambda issue: issue.labels,
    "regression":          lambda issue: issue.regression,
    "sla_days":            lambda issue: issue.sla_days,
    "sla_remaining_days":  lambda issue: issue.sla_remaining_days,
    "sla_pct_used":        lambda issue: issue.sla_pct_used,
    "sla_status":          lambda issue: issue.sla_status,
}


class RuleEngine:
    def __init__(
        self,
        jira_client: JiraClient,
        people_registry: PeopleRegistry | None = None,
        max_issues_per_rule: int = 100,
        project_filter: list[str] | None = None,
        issue_type_filter: list[str] | None = None,
    ):
        self._jira = jira_client
        self._registry = people_registry
        self._max = max_issues_per_rule
        self._project_filter = project_filter        # e.g. ["PMN", "CXDV"]
        self._issue_type_filter = issue_type_filter  # e.g. ["Epic", "Bug", "Story"]
        # Comment cache: issue_key -> list[IssueComment]  (lives for one run)
        self._comment_cache: dict[str, list[IssueComment]] = {}
        self._project_lead_cache: dict[str, "JiraUser | None"] = {}

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def evaluate_all(self, rules: list[RuleConfig]) -> list[AlertGroup]:
        """Run every rule and return one AlertGroup per owner."""
        self._comment_cache.clear()
        self._project_lead_cache.clear()
        all_matches: list[RuleMatch] = []
        for rule in rules:
            all_matches.extend(self.evaluate_rule(rule))
        return _merge_into_groups(all_matches)

    def _apply_project_filter(self, jql: str) -> str:
        """Prepend 'project in (P1, P2) AND' to the rule JQL when a filter is set."""
        if not self._project_filter:
            return jql
        projects = ", ".join(self._project_filter)
        return f"project in ({projects}) AND ({jql})"

    def _apply_issue_type_filter(self, jql: str) -> str:
        """Prepend 'issuetype in (Epic, Bug, Story) AND' when a type filter is set."""
        if not self._issue_type_filter:
            return jql
        types = ", ".join(self._issue_type_filter)
        return f"issuetype in ({types}) AND ({jql})"

    def evaluate_rule(self, rule: RuleConfig) -> list[RuleMatch]:
        if not rule.enabled:
            return []

        logger.info("Evaluating rule '%s'", rule.id)
        jql = self._apply_project_filter(rule.jql)
        jql = self._apply_issue_type_filter(jql)
        issues = self._jira.search_issues(jql, max_results=self._max)

        if rule.group_by == GROUP_BY_NOTIFY_ROLE:
            matches = self._evaluate_notify_role_rule(rule, issues)
        else:
            matches = self._evaluate_standard_rule(rule, issues)

        logger.info("Rule '%s': %d match(es)", rule.id, len(matches))
        return matches

    # ------------------------------------------------------------------
    # Standard grouping (by assignee / reporter)
    # ------------------------------------------------------------------

    def _evaluate_standard_rule(
        self, rule: RuleConfig, issues: list[JiraIssue]
    ) -> list[RuleMatch]:
        matches = []
        for issue in issues:
            ctx = self._check_conditions(rule, issue, notified_person=None)
            if ctx is not None:
                match = RuleMatch(rule=rule, issue=issue, context=ctx)
                # Route unassigned issues to the fallback owner if configured
                if (
                    rule.group_by == GROUP_BY_ASSIGNEE
                    and issue.assignee is None
                    and rule.fallback_assignee_role
                ):
                    if rule.fallback_assignee_role == "project_lead":
                        # Resolve project lead live from Jira (cached per project per run)
                        lead = self._get_project_lead_cached(issue.key.split("-")[0])
                        if lead:
                            # Jira's project endpoint often omits the lead's email, which
                            # leaves the digest undeliverable. Enrich from the registry
                            # (matched by account_id) so the owner carries a real email.
                            if self._registry:
                                lead_person = self._registry.resolve_jira_user(lead)
                                if lead_person:
                                    lead = lead_person.to_jira_user()
                            match.notified_person_key = lead.account_id or lead.email
                            match.owner_override = lead
                    elif self._registry:
                        fallback = self._registry.get_by_roles([rule.fallback_assignee_role])
                        if fallback:
                            person = fallback[0]
                            match.notified_person_key = person.jira_account_id or person.email
                            match.owner_override = person.to_jira_user()

                # Skip people who are configured to receive role-based alerts only
                # (e.g. product managers who should only get epics_in_validation alerts).
                if self._registry and rule.group_by in (GROUP_BY_ASSIGNEE, GROUP_BY_REPORTER):
                    owner_jira_user = (
                        issue.assignee if rule.group_by == GROUP_BY_ASSIGNEE else issue.reporter
                    )
                    if match.owner_override is None and owner_jira_user:
                        owner_person = self._registry.resolve_jira_user(owner_jira_user)
                        if owner_person and owner_person.role_alerts_only:
                            logger.debug(
                                "Skipping %s for rule '%s' — role_alerts_only",
                                owner_person.display_name,
                                rule.id,
                            )
                            continue

                matches.append(match)
        return matches

    # ------------------------------------------------------------------
    # Role-based fan-out (group_by: notify_role)
    # ------------------------------------------------------------------

    def _evaluate_notify_role_rule(
        self, rule: RuleConfig, issues: list[JiraIssue]
    ) -> list[RuleMatch]:
        if not self._registry:
            logger.warning(
                "Rule '%s' uses notify_role but no people registry is configured — skipping",
                rule.id,
            )
            return []

        recipients = self._registry.get_by_roles(rule.notify_roles)
        if not recipients:
            logger.warning(
                "Rule '%s': no people found with roles %s", rule.id, rule.notify_roles
            )
            return []

        matches = []
        for issue in issues:
            for person in recipients:
                ctx = self._check_conditions(rule, issue, notified_person=person)
                if ctx is not None:
                    matches.append(
                        RuleMatch(
                            rule=rule,
                            issue=issue,
                            context=ctx,
                            notified_person_key=person.email or person.display_name,
                            owner_override=person.to_jira_user(),
                        )
                    )
        return matches

    # ------------------------------------------------------------------
    # Condition checking
    # ------------------------------------------------------------------

    def _check_conditions(
        self,
        rule: RuleConfig,
        issue: JiraIssue,
        notified_person: Person | None,
    ) -> dict | None:
        """Return a populated context dict if ALL conditions pass, else None."""
        context: dict = {}

        for condition in rule.conditions:
            field = condition["field"]
            operator = condition["operator"]
            threshold = condition.get("value")

            if is_role_aware(operator):
                passed, extra_ctx = self._evaluate_role_aware_condition(
                    condition, issue, notified_person, rule
                )
                if not passed:
                    return None
                context.update(extra_ctx)
            else:
                extractor = _FIELD_EXTRACTORS.get(field)
                actual = extractor(issue) if extractor else issue.raw.get("fields", {}).get(field)
                if not evaluate_condition(actual, operator, threshold):
                    return None
                context[field] = actual
                context[f"{field}_threshold"] = threshold

        # Populate convenience keys available in message templates
        context.setdefault("age_days", issue.age_days)
        context.setdefault("days_since_update", issue.days_since_update)
        context.setdefault("cycle_time_days", issue.cycle_time_days)
        context.setdefault("threshold", rule.conditions[0].get("value") if rule.conditions else None)
        context["priority"] = issue.priority
        context["status"] = issue.status
        context["sla_days"] = issue.sla_days
        context["sla_remaining_days"] = issue.sla_remaining_days
        context["sla_pct_used"] = issue.sla_pct_used
        context["sla_pct_used_pct"] = int(round(issue.sla_pct_used * 100)) if issue.sla_pct_used is not None else None
        context["sla_status"] = issue.sla_status
        context["regression"] = issue.regression
        context["sla_overdue_days"] = max(0, issue.age_days - (issue.sla_days or 0)) if issue.sla_days else 0

        return context

    # ------------------------------------------------------------------
    # Role-aware condition: days_since_role_comment
    # ------------------------------------------------------------------

    def _evaluate_role_aware_condition(
        self,
        condition: dict,
        issue: JiraIssue,
        notified_person: Person | None,
        rule: RuleConfig,
    ) -> tuple[bool, dict]:
        """
        Operator: days_since_role_comment
        ───────────────────────────────────
        condition:
          field: days_since_role_comment
          operator: days_since_role_comment
          value: 7           # threshold in days
          role: product_manager   # which role must have commented

        The condition PASSES (issue is included in the alert) when NO person
        with the given role has commented within <value> days — i.e. feedback
        is overdue.

        Returns (passed: bool, extra_ctx: dict)
        """
        threshold_days: int = int(condition.get("value", 0))
        target_role: str = condition.get("role", "")
        unit: str = str(condition.get("unit", "calendar_days")).lower()
        use_business_days = unit in ("business_days", "business", "business_day")

        if not target_role and not rule.notify_roles:
            logger.warning("days_since_role_comment needs 'role' key in condition")
            return False, {}

        role = target_role or (rule.notify_roles[0] if rule.notify_roles else "")

        # Resolve the set of account IDs / emails that hold this role
        role_identifiers = self._role_identifiers(role, notified_person)

        comments = self._get_comments_cached(issue.key)
        now = datetime.utcnow()

        # Find the most recent comment from anyone in the role
        last_role_comment: datetime | None = None
        for comment in comments:
            if self._comment_is_from_role(comment, role_identifiers):
                if last_role_comment is None or comment.created_at > last_role_comment:
                    last_role_comment = comment.created_at

        # Alert fires when there's NO recent comment from the role — i.e. more
        # than <threshold> days (calendar or business) have elapsed, or the role
        # has never commented at all.
        if last_role_comment is None:
            days_since = None
            no_recent_comment = True
        elif use_business_days:
            days_since = _business_days_between(last_role_comment, now)
            no_recent_comment = days_since > threshold_days
        else:
            days_since = (now - last_role_comment).days
            no_recent_comment = last_role_comment < now - timedelta(days=threshold_days)

        extra = {
            "days_since_role_comment": days_since,
            "role_comment_threshold": threshold_days,
            "role_comment_unit": "business days" if use_business_days else "days",
            "role": role,
        }
        return no_recent_comment, extra

    def _role_identifiers(
        self, role: str, notified_person: Person | None
    ) -> set[str]:
        """Return the set of account_ids + emails for all people with the role."""
        if not self._registry:
            return set()
        identifiers: set[str] = set()
        for person in self._registry.get_by_role(role):
            if person.jira_account_id:
                identifiers.add(person.jira_account_id)
            if person.email:
                identifiers.add(person.email.lower())
        return identifiers

    def _comment_is_from_role(
        self, comment: IssueComment, identifiers: set[str]
    ) -> bool:
        return (
            comment.account_id in identifiers
            or comment.email.lower() in identifiers
        )

    def _get_project_lead_cached(self, project_key: str) -> "JiraUser | None":
        if project_key not in self._project_lead_cache:
            self._project_lead_cache[project_key] = self._jira.get_project_lead(project_key)
            lead = self._project_lead_cache[project_key]
            logger.info("Project lead for %s: %s", project_key, lead.display_name if lead else "not found")
        return self._project_lead_cache[project_key]

    def _get_comments_cached(self, issue_key: str) -> list[IssueComment]:
        if issue_key not in self._comment_cache:
            try:
                self._comment_cache[issue_key] = self._jira.get_comments(issue_key)
            except Exception as exc:
                logger.warning("Failed to fetch comments for %s: %s", issue_key, exc)
                self._comment_cache[issue_key] = []
        return self._comment_cache[issue_key]


# ---------------------------------------------------------------------------
# Grouping helpers
# ---------------------------------------------------------------------------

def _merge_into_groups(matches: list[RuleMatch]) -> list[AlertGroup]:
    """Merge all RuleMatches into one AlertGroup per owner key."""
    groups: dict[str, AlertGroup] = {}

    for match in matches:
        key = match.owner_key

        if key not in groups:
            # Resolve the owner JiraUser for this group
            owner = _resolve_owner(match)
            groups[key] = AlertGroup(owner=owner, owner_key=key)

        groups[key].matches.append(match)

    # Sort: named owners alphabetically, unassigned last
    return sorted(
        groups.values(),
        key=lambda g: (g.owner_key == "unassigned", g.display_name.lower()),
    )


def _resolve_owner(match: RuleMatch) -> JiraUser | None:
    """Return the JiraUser that should appear as the digest recipient."""
    if match.owner_override:
        return match.owner_override
    if match.rule.group_by == "assignee":
        return match.issue.assignee
    if match.rule.group_by == "reporter":
        return match.issue.reporter
    return None
