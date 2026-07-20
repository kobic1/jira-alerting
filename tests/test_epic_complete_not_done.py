"""Tests for the 'Epic Complete but Not Done' rule primitives:
all_children_done operator, business_days_since_update, and project_lead grouping.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from src.rules.engine import RuleEngine
from src.models import JiraIssue, JiraUser, RuleConfig, Severity


def _epic(key="CXDV-1", updated_days_ago=5):
    now = datetime.utcnow()
    return JiraIssue(
        key=key, summary="An epic", issue_type="Epic", status="Validation",
        priority=None, assignee=None, reporter=_u("rep"),
        created_at=now - timedelta(days=60),
        updated_at=now - timedelta(days=updated_days_ago),
        base_url="https://j",
    )


def _child(key, category):
    """A child issue whose statusCategory key is `category` (done/indeterminate/new)."""
    return JiraIssue(
        key=key, summary="child", issue_type="Story", status="Done" if category == "done" else "Open",
        priority=None, assignee=None, reporter=_u("rep"),
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(), base_url="https://j",
        raw={"fields": {"status": {"statusCategory": {"key": category}}}},
    )


def _u(uid, email=None):
    return JiraUser(account_id=uid, display_name=uid, email=email or f"{uid}@co.com")


def _rule():
    return RuleConfig(
        id="epic_complete_not_done", name="Completed Epic, Status Not Done", description="d",
        jql="issuetype = Epic AND status = Validation",
        conditions=[
            {"field": "all_children_done", "operator": "all_children_done", "value": True},
            {"field": "business_days_since_update", "operator": "gte", "value": 2},
        ],
        group_by="project_lead", severity=Severity.MEDIUM, message_template="{children_total} done",
    )


def _engine(children, lead=_u("lead", "lead@co.com")):
    jira = MagicMock()
    jira.search_issues.return_value = children          # child fetch returns these
    jira.get_project_lead.return_value = lead
    reg = MagicMock()
    reg.resolve_jira_user.return_value = None            # no enrichment; keep lead as-is
    return RuleEngine(jira_client=jira, people_registry=reg, project_filter=["CXDV"])


def test_matches_when_all_children_done_and_stale():
    eng = _engine([_child("C-1", "done"), _child("C-2", "done")])
    matches = eng._evaluate_standard_rule(_rule(), [_epic(updated_days_ago=5)])
    assert len(matches) == 1
    m = matches[0]
    assert m.context["children_total"] == 2
    assert m.context["children_done"] == 2
    assert m.owner.email == "lead@co.com"          # routed to project lead


def test_no_match_when_a_child_is_open():
    eng = _engine([_child("C-1", "done"), _child("C-2", "indeterminate")])
    matches = eng._evaluate_standard_rule(_rule(), [_epic(updated_days_ago=5)])
    assert matches == []


def test_matches_when_epic_has_no_children():
    # An epic in Validation with NO children and stale 2+ biz-days now qualifies
    # (no open work left under it).
    eng = _engine([])
    matches = eng._evaluate_standard_rule(_rule(), [_epic(updated_days_ago=5)])
    assert len(matches) == 1
    assert matches[0].context["children_total"] == 0
    assert matches[0].context["children_open"] == 0
    assert "no child issues" in matches[0].context["children_summary"]


def test_no_match_when_recently_changed():
    # All children done, but the epic changed today → business_days_since_update < 2.
    eng = _engine([_child("C-1", "done")])
    matches = eng._evaluate_standard_rule(_rule(), [_epic(updated_days_ago=0)])
    assert matches == []
