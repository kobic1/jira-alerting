"""Tests for preview mode — redirect, banner, and dedup bypass."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.delivery.deduplication import DeduplicationStore
from src.delivery.dispatcher import AlertDispatcher
from src.delivery.teams import TeamsWebhookSender
from src.messaging.formatter import MessageFormatter
from src.models import AlertGroup, JiraIssue, JiraUser, RuleConfig, RuleMatch, Severity


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _user(uid="u1", name="Alice", email="alice@co.com"):
    return JiraUser(account_id=uid, display_name=name, email=email)


def _reviewer():
    return JiraUser(account_id="rev1", display_name="Kobi Cohen", email="kobi@co.com")


def _issue(key="PROJ-1", assignee=None):
    now = datetime.utcnow()
    return JiraIssue(
        key=key, summary="A bug", issue_type="Bug", status="New", priority="High",
        assignee=assignee,
        reporter=_user("r1", "Reporter", "rep@co.com"),
        created_at=now - timedelta(days=10), updated_at=now - timedelta(days=10),
        base_url="https://example.atlassian.net",
    )


def _rule(rid="r1"):
    return RuleConfig(
        id=rid, name="Prod Bugs", description="desc",
        jql="project=PROD", conditions=[],
        group_by="assignee", severity=Severity.HIGH,
        message_template="Age: {age_days}",
    )


def _group(owner_name="Alice", issue_key="PROJ-1"):
    owner = _user(uid=f"u_{owner_name}", name=owner_name)
    issue = _issue(key=issue_key, assignee=owner)
    match = RuleMatch(rule=_rule(), issue=issue, context={"age_days": 10, "threshold": 5})
    return AlertGroup(owner=owner, owner_key=owner.account_id, matches=[match])


def _make_dispatcher(tmp_path, preview_recipient=None, dry_run=False):
    sender = MagicMock(spec=TeamsWebhookSender)
    sender.send.return_value = True
    formatter = MagicMock(spec=MessageFormatter)
    formatter.format_digest.return_value = {"type": "message", "attachments": []}
    dedup = DeduplicationStore(window_hours=24, store_path=str(tmp_path / "cache.json"))
    dispatcher = AlertDispatcher(
        sender=sender, formatter=formatter, dedup=dedup,
        dry_run=dry_run, preview_recipient=preview_recipient,
    )
    return dispatcher, sender, formatter, dedup


# ── Preview mode: redirect ────────────────────────────────────────────────────

def test_preview_sends_all_groups_to_reviewer(tmp_path):
    reviewer = _reviewer()
    dispatcher, sender, formatter, _ = _make_dispatcher(tmp_path, preview_recipient=reviewer)

    alice_group = _group("Alice", "A-1")
    bob_group = _group("Bob", "B-1")
    dispatcher.dispatch([alice_group, bob_group])

    # Two cards sent (one per real recipient)
    assert sender.send.call_count == 2


def test_preview_format_digest_called_with_preview_for(tmp_path):
    reviewer = _reviewer()
    dispatcher, sender, formatter, _ = _make_dispatcher(tmp_path, preview_recipient=reviewer)

    dispatcher.dispatch([_group("Alice", "A-1")])

    call_kwargs = formatter.format_digest.call_args
    assert call_kwargs.kwargs.get("preview_for") == "Kobi Cohen"


def test_normal_mode_does_not_set_preview_for(tmp_path):
    dispatcher, sender, formatter, _ = _make_dispatcher(tmp_path, preview_recipient=None)
    dispatcher.dispatch([_group("Alice", "A-1")])

    call_kwargs = formatter.format_digest.call_args
    assert call_kwargs.kwargs.get("preview_for") is None


# ── Preview mode: dedup bypass ────────────────────────────────────────────────

def test_preview_bypasses_dedup_filter(tmp_path):
    """Even if the issue was already alerted, preview shows it anyway."""
    reviewer = _reviewer()
    dispatcher, sender, formatter, dedup = _make_dispatcher(tmp_path, preview_recipient=reviewer)

    # Pre-mark the issue as already sent
    dedup.mark_alerted("r1", "PROJ-1")

    group = _group("Alice", "PROJ-1")
    stats = dispatcher.dispatch([group])

    # Preview should still send it
    assert sender.send.call_count == 1
    assert stats["groups_sent"] == 1
    assert stats["groups_skipped_dedup"] == 0


def test_normal_mode_respects_dedup(tmp_path):
    dispatcher, sender, formatter, dedup = _make_dispatcher(tmp_path, preview_recipient=None)
    dedup.mark_alerted("r1", "PROJ-1")

    stats = dispatcher.dispatch([_group("Alice", "PROJ-1")])

    assert sender.send.call_count == 0
    assert stats["groups_skipped_dedup"] == 1


# ── Preview mode: no dedup footprint ─────────────────────────────────────────

def test_preview_does_not_mark_alerted(tmp_path):
    """After a preview run, the real run must still fire for the same issues."""
    reviewer = _reviewer()
    dispatcher, sender, formatter, dedup = _make_dispatcher(tmp_path, preview_recipient=reviewer)

    dispatcher.dispatch([_group("Alice", "PROJ-1")])

    # The issue must NOT be in the dedup store
    assert not dedup.is_duplicate("r1", "PROJ-1")


def test_normal_run_marks_alerted(tmp_path):
    dispatcher, sender, formatter, dedup = _make_dispatcher(tmp_path, preview_recipient=None)
    dispatcher.dispatch([_group("Alice", "PROJ-1")])
    assert dedup.is_duplicate("r1", "PROJ-1")


# ── Preview banner in formatter ───────────────────────────────────────────────

def test_preview_banner_appears_in_message():
    from src.ingestion.jira_client import JiraClient
    jira = MagicMock(spec=JiraClient)
    jira.get_filter_url.return_value = "https://example.atlassian.net/issues"
    from src.messaging.formatter import MessageFormatter
    fmt = MessageFormatter(jira_client=jira)

    owner = _user()
    issue = _issue(assignee=owner)
    match = RuleMatch(rule=_rule(), issue=issue, context={"age_days": 10, "threshold": 5, "status": "New"})
    group = AlertGroup(owner=owner, owner_key=owner.account_id, matches=[match])

    msg_preview = fmt.format_digest(group, preview_for="Kobi Cohen")["message"]
    msg_normal  = fmt.format_digest(group, preview_for=None)["message"]

    assert "PREVIEW" in msg_preview
    assert "Kobi Cohen" in msg_preview
    assert "Alice" in msg_preview
    assert "PREVIEW" not in msg_normal


def test_preview_banner_not_in_normal_message():
    from src.ingestion.jira_client import JiraClient
    jira = MagicMock(spec=JiraClient)
    jira.get_filter_url.return_value = "https://example.atlassian.net/issues"
    from src.messaging.formatter import MessageFormatter
    fmt = MessageFormatter(jira_client=jira)

    owner = _user()
    issue = _issue(assignee=owner)
    match = RuleMatch(rule=_rule(), issue=issue, context={"age_days": 10, "threshold": 5, "status": "New"})
    group = AlertGroup(owner=owner, owner_key=owner.account_id, matches=[match])

    payload = fmt.format_digest(group, preview_for=None)
    assert "PREVIEW" not in payload["message"]


# ── Stats ─────────────────────────────────────────────────────────────────────

def test_preview_stats_report_mode(tmp_path):
    reviewer = _reviewer()
    dispatcher, _, _, _ = _make_dispatcher(tmp_path, preview_recipient=reviewer)
    stats = dispatcher.dispatch([_group("Alice", "A-1")])
    assert stats["mode"] == "preview"


def test_normal_stats_report_mode(tmp_path):
    dispatcher, _, _, _ = _make_dispatcher(tmp_path, preview_recipient=None)
    stats = dispatcher.dispatch([_group("Alice", "A-1")])
    assert stats["mode"] == "normal"
