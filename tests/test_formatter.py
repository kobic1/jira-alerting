"""Tests for the MessageFormatter HTML digest."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from src.messaging.formatter import MessageFormatter
from src.models import AlertGroup, JiraIssue, JiraUser, RuleConfig, RuleMatch, Severity


def _user(uid="u1", name="Alice", email="alice@co.com"):
    return JiraUser(account_id=uid, display_name=name, email=email)


def _issue(key="PROJ-1"):
    now = datetime.utcnow()
    return JiraIssue(
        key=key, summary="A test issue", issue_type="Bug", status="New",
        priority="High", assignee=_user(), reporter=_user("r1", "Reporter"),
        created_at=now - timedelta(days=10), updated_at=now - timedelta(days=10),
        base_url="https://example.atlassian.net",
    )


def _rule(rid="r1", severity=Severity.HIGH):
    return RuleConfig(
        id=rid, name="Production Bugs", description="Bugs open > 5 days",
        jql="project = PROD", conditions=[], group_by="assignee",
        severity=severity, message_template="In {status} for {age_days} days",
    )


def _match(rule=None, issue=None):
    r = rule or _rule()
    i = issue or _issue()
    return RuleMatch(rule=r, issue=i, context={"age_days": 10, "threshold": 5, "status": "New"})


def _formatter():
    jira = MagicMock()
    jira.get_filter_url.return_value = "https://example.atlassian.net/issues"
    return MessageFormatter(jira_client=jira)


def test_returns_dict_with_recipient_and_message():
    fmt = _formatter()
    group = AlertGroup(owner=_user(), owner_key="u1", matches=[_match()])
    payload = fmt.format_digest(group)
    assert "recipient" in payload
    assert "message" in payload


def test_recipient_is_owners_email():
    fmt = _formatter()
    owner = _user(email="bob@co.com")
    group = AlertGroup(owner=owner, owner_key="u1", matches=[_match()])
    payload = fmt.format_digest(group)
    assert payload["recipient"] == "bob@co.com"


def test_message_is_html_string():
    fmt = _formatter()
    group = AlertGroup(owner=_user(), owner_key="u1", matches=[_match()])
    payload = fmt.format_digest(group)
    assert isinstance(payload["message"], str)
    assert "<" in payload["message"]


def test_message_contains_persons_name():
    fmt = _formatter()
    group = AlertGroup(owner=_user(name="Bob Smith"), owner_key="u1", matches=[_match()])
    payload = fmt.format_digest(group)
    assert "Bob Smith" in payload["message"]


def test_message_contains_issue_key_and_link():
    fmt = _formatter()
    group = AlertGroup(owner=_user(), owner_key="u1", matches=[_match(issue=_issue("PROJ-999"))])
    payload = fmt.format_digest(group)
    assert "PROJ-999" in payload["message"]
    assert "href" in payload["message"]


def test_message_contains_rule_name():
    fmt = _formatter()
    group = AlertGroup(owner=_user(), owner_key="u1", matches=[_match(rule=_rule(rid="r1"))])
    payload = fmt.format_digest(group)
    assert "Production Bugs" in payload["message"]


def test_rules_sorted_high_before_low():
    fmt = _formatter()
    high_match = _match(rule=_rule("r_high", Severity.HIGH), issue=_issue("H-1"))
    low_match  = _match(rule=_rule("r_low",  Severity.LOW),  issue=_issue("L-1"))
    group = AlertGroup(owner=_user(), owner_key="u1", matches=[low_match, high_match])
    payload = fmt.format_digest(group)
    assert payload["message"].index("🔴") < payload["message"].index("🔵")


def test_preview_banner_present_when_preview_for_set():
    fmt = _formatter()
    group = AlertGroup(owner=_user(name="Shikha"), owner_key="u1", matches=[_match()])
    payload = fmt.format_digest(group, preview_for="Kobi Cohen")
    assert "PREVIEW" in payload["message"]
    assert "Kobi Cohen" in payload["message"]
    assert "Shikha" in payload["message"]


def test_no_preview_banner_in_normal_mode():
    fmt = _formatter()
    group = AlertGroup(owner=_user(), owner_key="u1", matches=[_match()])
    payload = fmt.format_digest(group, preview_for=None)
    assert "PREVIEW" not in payload["message"]


def test_no_snooze_link_when_url_not_configured():
    fmt = _formatter()  # no snooze_flow_url
    group = AlertGroup(owner=_user(), owner_key="u1", matches=[_match()])
    payload = fmt.format_digest(group)
    assert "Snooze" not in payload["message"]


def test_snooze_link_present_and_carries_context_when_configured():
    jira = MagicMock()
    jira.get_filter_url.return_value = "https://example.atlassian.net/issues"
    fmt = MessageFormatter(
        jira_client=jira,
        snooze_flow_url="https://flow.example.com/invoke?api-version=1",
    )
    owner = _user(email="bob@co.com")
    group = AlertGroup(owner=owner, owner_key="u1", matches=[_match(issue=_issue("PROJ-42"))])
    msg = fmt.format_digest(group)["message"]
    assert "⏰ Snooze 2h" in msg
    # The link must carry recipient + message (the identical flow's two fields).
    assert "recipient=bob%40co.com" in msg  # url-encoded @
    assert "&message=" in msg
    assert "PROJ-42" in msg  # issue key appears (url-encoded) inside the message param


def test_no_snooze_link_when_owner_has_no_email():
    jira = MagicMock()
    jira.get_filter_url.return_value = "https://example.atlassian.net/issues"
    fmt = MessageFormatter(jira_client=jira, snooze_flow_url="https://flow.example.com/x")
    owner = JiraUser(account_id="u1", display_name="No Email", email=None)
    group = AlertGroup(owner=owner, owner_key="u1", matches=[_match()])
    msg = fmt.format_digest(group)["message"]
    assert "Snooze" not in msg


def test_multiple_rules_all_appear_in_message():
    fmt = _formatter()
    m1 = _match(rule=_rule("r1", Severity.HIGH),   issue=_issue("A-1"))
    m2 = _match(rule=_rule("r2", Severity.MEDIUM), issue=_issue("B-2"))
    group = AlertGroup(owner=_user(), owner_key="u1", matches=[m1, m2])
    payload = fmt.format_digest(group)
    assert "A-1" in payload["message"]
    assert "B-2" in payload["message"]
