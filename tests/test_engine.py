from datetime import datetime, timedelta
from unittest.mock import MagicMock

from src.models import JiraIssue, JiraUser, RuleConfig, Severity
from src.rules.engine import RuleEngine


def _make_issue(key="PROJ-1", age_days=20, days_since_update=10, assignee=None):
    now = datetime.utcnow()
    return JiraIssue(
        key=key,
        summary="Test issue",
        issue_type="Story",
        status="In Progress",
        priority="Medium",
        assignee=assignee,
        reporter=JiraUser("r1", "Reporter One", "r@example.com"),
        created_at=now - timedelta(days=age_days),
        updated_at=now - timedelta(days=days_since_update),
        base_url="https://example.atlassian.net",
    )


def _make_rule(**kwargs) -> RuleConfig:
    defaults = dict(
        id="test_rule",
        name="Test Rule",
        description="",
        jql="project = TEST",
        conditions=[{"field": "age_days", "operator": "gt", "value": 14}],
        group_by="assignee",
        severity=Severity.MEDIUM,
        message_template="Age: {age_days}",
        enabled=True,
    )
    defaults.update(kwargs)
    return RuleConfig(**defaults)


def test_matching_issue():
    client = MagicMock()
    client.search_issues.return_value = [_make_issue(age_days=20)]
    engine = RuleEngine(client)
    rule = _make_rule()
    matches = engine.evaluate_rule(rule)
    assert len(matches) == 1
    assert matches[0].issue.key == "PROJ-1"


def test_non_matching_issue():
    client = MagicMock()
    client.search_issues.return_value = [_make_issue(age_days=5)]
    engine = RuleEngine(client)
    rule = _make_rule()
    matches = engine.evaluate_rule(rule)
    assert matches == []


def test_disabled_rule():
    client = MagicMock()
    client.search_issues.return_value = [_make_issue(age_days=30)]
    engine = RuleEngine(client)
    rule = _make_rule(enabled=False)
    matches = engine.evaluate_rule(rule)
    assert matches == []


def test_grouping_by_assignee():
    assignee_a = JiraUser("u1", "Alice", "alice@example.com")
    assignee_b = JiraUser("u2", "Bob", "bob@example.com")
    client = MagicMock()
    client.search_issues.return_value = [
        _make_issue("A-1", assignee=assignee_a),
        _make_issue("A-2", assignee=assignee_b),
        _make_issue("A-3", assignee=assignee_a),
    ]
    engine = RuleEngine(client)
    rule = _make_rule()
    groups = engine.evaluate_all([rule])
    assert len(groups) == 2
    alice_group = next(g for g in groups if g.owner_key == "u1")
    assert len(alice_group.matches) == 2


def test_unassigned_goes_to_unassigned_group():
    client = MagicMock()
    client.search_issues.return_value = [_make_issue("U-1", assignee=None)]
    engine = RuleEngine(client)
    rule = _make_rule()
    groups = engine.evaluate_all([rule])
    assert groups[0].owner_key == "unassigned"
