"""Tests for notify_role fan-out and days_since_role_comment condition."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.ingestion.jira_client import IssueComment
from src.ingestion.people_registry import Person, PeopleRegistry
from src.models import AlertGroup, JiraIssue, JiraUser, RuleConfig, Severity, GROUP_BY_NOTIFY_ROLE
from src.rules.engine import RuleEngine


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pm(name="Shikha", email="shikha@co.com", account_id="pm1"):
    return Person(display_name=name, email=email, jira_account_id=account_id,
                  roles=["product_manager"])


def _registry(*people):
    return PeopleRegistry(list(people))


def _epic(key="EPIC-1", status="Validation"):
    now = datetime.utcnow()
    return JiraIssue(
        key=key, summary="Big feature epic", issue_type="Epic",
        status=status, priority="High",
        assignee=JiraUser("dev1", "Dev One", "dev@co.com"),
        reporter=JiraUser("rep1", "Rep One", "rep@co.com"),
        created_at=now - timedelta(days=30),
        updated_at=now - timedelta(days=10),
        base_url="https://example.atlassian.net",
    )


def _notify_role_rule(conditions=None):
    return RuleConfig(
        id="epics_validation",
        name="Epics Awaiting PM Feedback",
        description="",
        jql='issuetype = Epic AND status = Validation',
        conditions=conditions or [
            {"field": "days_since_role_comment", "operator": "days_since_role_comment",
             "value": 7, "role": "product_manager"}
        ],
        group_by=GROUP_BY_NOTIFY_ROLE,
        notify_roles=["product_manager"],
        severity=Severity.HIGH,
        message_template="No PM feedback for {role_comment_threshold}+ days",
    )


def _engine(people=None, comments_by_key=None):
    jira = MagicMock()
    jira.search_issues.return_value = [_epic()]

    def get_comments(key):
        return (comments_by_key or {}).get(key, [])
    jira.get_comments.side_effect = get_comments

    registry = _registry(*people) if people else _registry(_pm())
    return RuleEngine(jira_client=jira, people_registry=registry), jira


# ── Fan-out tests ─────────────────────────────────────────────────────────────

def test_notify_role_creates_one_group_per_pm():
    pm1 = _pm("Shikha", "s@co.com", "pm1")
    pm2 = _pm("Jane", "j@co.com", "pm2")
    engine, jira = _engine(people=[pm1, pm2], comments_by_key={"EPIC-1": []})
    rule = _notify_role_rule()
    groups = engine.evaluate_all([rule])
    # Both PMs should each receive a digest containing EPIC-1
    assert len(groups) == 2
    names = {g.display_name for g in groups}
    assert "Shikha" in names
    assert "Jane" in names


def test_each_pm_group_contains_the_epic():
    engine, _ = _engine(comments_by_key={"EPIC-1": []})
    rule = _notify_role_rule()
    groups = engine.evaluate_all([rule])
    assert len(groups) == 1
    assert groups[0].matches[0].issue.key == "EPIC-1"


def test_no_registry_skips_notify_role_rule():
    jira = MagicMock()
    jira.search_issues.return_value = [_epic()]
    engine = RuleEngine(jira_client=jira, people_registry=None)
    rule = _notify_role_rule()
    matches = engine.evaluate_rule(rule)
    assert matches == []


def test_no_people_with_role_skips():
    em_only = Person("Nobody", "n@co.com", roles=["engineering_manager"])
    engine, _ = _engine(people=[em_only])
    rule = _notify_role_rule()
    matches = engine.evaluate_rule(rule)
    assert matches == []


# ── days_since_role_comment condition ────────────────────────────────────────

def test_alert_fires_when_no_pm_comment():
    """No comment at all → condition passes → issue appears in digest."""
    engine, _ = _engine(comments_by_key={"EPIC-1": []})
    rule = _notify_role_rule()
    matches = engine.evaluate_rule(rule)
    assert len(matches) == 1


def test_alert_suppressed_when_recent_pm_comment():
    """PM commented 2 days ago (within 7-day threshold) → no alert."""
    pm = _pm(account_id="pm1")
    recent_comment = IssueComment(
        account_id="pm1", display_name="Shikha",
        email="shikha@co.com",
        created_at=datetime.utcnow() - timedelta(days=2),
    )
    engine, _ = _engine(people=[pm], comments_by_key={"EPIC-1": [recent_comment]})
    rule = _notify_role_rule()
    matches = engine.evaluate_rule(rule)
    assert matches == []


def test_alert_fires_when_pm_comment_is_stale():
    """PM commented 10 days ago (past 7-day threshold) → alert fires."""
    pm = _pm(account_id="pm1")
    old_comment = IssueComment(
        account_id="pm1", display_name="Shikha",
        email="shikha@co.com",
        created_at=datetime.utcnow() - timedelta(days=10),
    )
    engine, _ = _engine(people=[pm], comments_by_key={"EPIC-1": [old_comment]})
    rule = _notify_role_rule()
    matches = engine.evaluate_rule(rule)
    assert len(matches) == 1


def test_non_pm_comment_does_not_suppress_alert():
    """A developer's comment doesn't count as PM feedback."""
    pm = _pm(account_id="pm1", email="pm@co.com")
    dev_comment = IssueComment(
        account_id="dev99", display_name="Dev",
        email="dev@co.com",
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    engine, _ = _engine(people=[pm], comments_by_key={"EPIC-1": [dev_comment]})
    rule = _notify_role_rule()
    matches = engine.evaluate_rule(rule)
    assert len(matches) == 1


def test_comment_cache_avoids_duplicate_api_calls():
    """Two PMs → one epic → get_comments called once, not twice."""
    pm1 = _pm("Shikha", "s@co.com", "pm1")
    pm2 = _pm("Jane", "j@co.com", "pm2")
    jira = MagicMock()
    jira.search_issues.return_value = [_epic()]
    jira.get_comments.return_value = []
    registry = _registry(pm1, pm2)
    engine = RuleEngine(jira_client=jira, people_registry=registry)
    engine.evaluate_rule(_notify_role_rule())
    jira.get_comments.assert_called_once_with("EPIC-1")


def test_context_contains_role_comment_fields():
    engine, _ = _engine(comments_by_key={"EPIC-1": []})
    rule = _notify_role_rule()
    matches = engine.evaluate_rule(rule)
    assert "role_comment_threshold" in matches[0].context
    assert matches[0].context["role_comment_threshold"] == 7
