"""Tests for the AlertDispatcher — verifies dedup, delivery, and dry-run."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.delivery.dispatcher import AlertDispatcher
from src.delivery.deduplication import DeduplicationStore
from src.delivery.teams import TeamsPowerAutomateSender, TeamsWebhookSender
from src.models import AlertGroup, JiraIssue, JiraUser, RuleConfig, RuleMatch, Severity


def _user(uid="u1", name="Alice"):
    return JiraUser(account_id=uid, display_name=name, email=f"{name.lower()}@example.com")


def _issue(key="PROJ-1", assignee=None):
    now = datetime.utcnow()
    return JiraIssue(
        key=key,
        summary="Test issue",
        issue_type="Bug",
        status="New",
        priority="High",
        assignee=assignee,
        reporter=_user("r1", "Reporter"),
        created_at=now - timedelta(days=10),
        updated_at=now - timedelta(days=10),
        base_url="https://example.atlassian.net",
    )


def _rule(rid="r1"):
    return RuleConfig(
        id=rid,
        name="Test Rule",
        description="desc",
        jql="project = TEST",
        conditions=[],
        group_by="assignee",
        severity=Severity.HIGH,
        message_template="Age: {age_days}",
    )


def _group(owner=None, matches=None):
    user = owner or _user()
    return AlertGroup(
        owner=user,
        owner_key=user.account_id,
        matches=matches or [RuleMatch(rule=_rule(), issue=_issue(assignee=user), context={"age_days": 10, "threshold": 5})],
    )


def _make_dispatcher(tmp_path, dry_run=False):
    sender = MagicMock(spec=TeamsWebhookSender)
    sender.send.return_value = True
    formatter = MagicMock()
    formatter.format_digest.return_value = {"type": "message", "attachments": []}
    dedup = DeduplicationStore(window_hours=24, store_path=str(tmp_path / "cache.json"))
    return AlertDispatcher(sender=sender, formatter=formatter, dedup=dedup, dry_run=dry_run), sender, formatter, dedup


def test_sends_one_message_per_group(tmp_path):
    dispatcher, sender, formatter, _ = _make_dispatcher(tmp_path)
    alice = _user("u1", "Alice")
    bob = _user("u2", "Bob")
    groups = [
        AlertGroup(owner=alice, owner_key="u1",
                   matches=[RuleMatch(rule=_rule(), issue=_issue("A-1", assignee=alice),
                                      context={"age_days": 10, "threshold": 5})]),
        AlertGroup(owner=bob, owner_key="u2",
                   matches=[RuleMatch(rule=_rule(), issue=_issue("B-1", assignee=bob),
                                      context={"age_days": 10, "threshold": 5})]),
    ]
    stats = dispatcher.dispatch(groups)
    assert formatter.format_digest.call_count == 2
    assert sender.send.call_count == 2
    assert stats["groups_sent"] == 2
    assert stats["issues_sent"] == 2


def test_dedup_suppresses_already_alerted(tmp_path):
    dispatcher, sender, formatter, dedup = _make_dispatcher(tmp_path)
    # Pre-mark the issue as already alerted
    dedup.mark_alerted("r1", "PROJ-1")
    group = _group()
    stats = dispatcher.dispatch([group])
    assert sender.send.call_count == 0
    assert stats["groups_skipped_dedup"] == 1
    assert stats["issues_skipped_dedup"] == 1


def test_marks_issues_alerted_after_success(tmp_path):
    dispatcher, sender, _, dedup = _make_dispatcher(tmp_path)
    group = _group()
    dispatcher.dispatch([group])
    assert dedup.is_duplicate("r1", "PROJ-1") is True


def test_does_not_mark_alerted_on_failure(tmp_path):
    dispatcher, sender, _, dedup = _make_dispatcher(tmp_path)
    sender.send.return_value = False
    group = _group()
    dispatcher.dispatch([group])
    assert dedup.is_duplicate("r1", "PROJ-1") is False


def test_dry_run_does_not_send(tmp_path):
    dispatcher, sender, formatter, dedup = _make_dispatcher(tmp_path, dry_run=True)
    group = _group()
    stats = dispatcher.dispatch([group])
    sender.send.assert_not_called()
    assert not dedup.is_duplicate("r1", "PROJ-1")


def test_multiple_rules_in_one_digest(tmp_path):
    dispatcher, sender, formatter, _ = _make_dispatcher(tmp_path)
    user = _user()
    rule_a, rule_b = _rule("ra"), _rule("rb")
    issue_a = _issue("A-1", assignee=user)
    issue_b = _issue("A-2", assignee=user)
    ctx = {"age_days": 10, "threshold": 5}
    group = AlertGroup(
        owner=user,
        owner_key=user.account_id,
        matches=[
            RuleMatch(rule=rule_a, issue=issue_a, context=ctx),
            RuleMatch(rule=rule_b, issue=issue_b, context=ctx),
        ],
    )
    stats = dispatcher.dispatch([group])
    # Only ONE digest message for this person, not two
    assert formatter.format_digest.call_count == 1
    assert sender.send.call_count == 1
    assert stats["issues_sent"] == 2


def _card_sender():
    """A Power Automate sender that CAN post cards (snooze flow configured)."""
    sender = MagicMock(spec=TeamsPowerAutomateSender)
    sender.supports_cards = True
    sender.send.return_value = True
    sender.send_card.return_value = True
    return sender


def _card_formatter():
    fmt = MagicMock()
    fmt.format_digest.return_value = {"recipient": "x@y.com", "message": "<b>html</b>"}
    fmt.format_digest_card.return_value = {"recipient": "x@y.com", "card": {"t": "AdaptiveCard"}, "message": "<b>html</b>"}
    return fmt


def test_live_multi_recipient_never_uses_card_path(tmp_path):
    """Safeguard: live sends must route via HTML (correctly addressed), not the
    fixed-recipient snooze flow — even when the sender supports cards."""
    sender = _card_sender()
    fmt = _card_formatter()
    dedup = DeduplicationStore(window_hours=24, store_path=str(tmp_path / "c.json"))
    dispatcher = AlertDispatcher(sender=sender, formatter=fmt, dedup=dedup)  # no preview → live

    dispatcher.dispatch([_group(owner=_user("u1", "Alice"))])

    assert fmt.format_digest.call_count == 1        # HTML built
    assert fmt.format_digest_card.call_count == 0    # card NOT built
    assert sender.send_card.call_count == 0          # snooze flow NOT used
    assert sender.send.call_count == 1               # HTML flow used


def test_preview_uses_card_path_when_supported(tmp_path):
    """In preview mode every digest goes to one reviewer, so the card path is safe."""
    sender = _card_sender()
    fmt = _card_formatter()
    dedup = DeduplicationStore(window_hours=24, store_path=str(tmp_path / "c.json"))
    reviewer = _user("rev", "Kobi")
    dispatcher = AlertDispatcher(sender=sender, formatter=fmt, dedup=dedup, preview_recipient=reviewer)

    dispatcher.dispatch([_group(owner=_user("u1", "Alice"))])

    assert fmt.format_digest_card.call_count == 1    # card built
    assert sender.send_card.call_count == 1          # snooze flow used
    assert sender.send.call_count == 0
