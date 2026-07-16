"""Dispatcher — deduplicates, formats, and delivers one digest per person.

Two modes
─────────
Normal  (`preview_recipient` is None)
    • Dedup filters are applied — already-alerted issues are skipped.
    • Cards are sent to the real owner of each AlertGroup.
    • Successful sends are recorded in the dedup store.

Preview (`preview_recipient` is a JiraUser)
    • Dedup is bypassed entirely — all issues appear regardless of history.
    • Every card is redirected to the single preview recipient (you).
    • A visible "👁️ PREVIEW" banner is prepended to each card identifying the
      real intended recipient.
    • Nothing is written to the dedup store, so the real run is unaffected.

Delivery flow (normal)
──────────────────────
  AlertGroup (one person + all their rule matches)
       │
       ├─ drop already-alerted (rule_id, issue_key) pairs
       ├─ if nothing new → skip
       ├─ format_digest() → Adaptive Card
       └─ send → on success, mark all pairs as alerted
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Union

from src.delivery.deduplication import DeduplicationStore
from src.delivery.teams import TeamsGraphSender, TeamsPowerAutomateSender, TeamsWebhookSender
from src.messaging.formatter import MessageFormatter
from src.models import AlertGroup, JiraUser, RuleMatch

logger = logging.getLogger(__name__)

Sender = Union[TeamsWebhookSender, TeamsGraphSender, TeamsPowerAutomateSender]


class AlertDispatcher:
    def __init__(
        self,
        sender: Sender,
        formatter: MessageFormatter,
        dedup: DeduplicationStore,
        dry_run: bool = False,
        preview_recipient: JiraUser | None = None,
        max_groups: int | None = None,
        sample_rules: bool = False,
    ):
        self._sender = sender
        self._formatter = formatter
        self._dedup = dedup
        self._dry_run = dry_run
        self._preview_recipient = preview_recipient
        self._max_groups = max_groups
        self._sample_rules = sample_rules  # send one message per rule with 1 example issue

    @property
    def is_preview(self) -> bool:
        return self._preview_recipient is not None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def dispatch(self, groups: list[AlertGroup]) -> dict[str, int]:
        """Send one digest message per person. Returns delivery statistics."""
        if self._sample_rules and self.is_preview:
            groups = self._build_sample_groups(groups)

        groups = self._apply_per_rule_limit(groups, limit=10)

        run_date = datetime.utcnow()
        stats: dict[str, int] = {
            "mode": "preview" if self.is_preview else "normal",
            "groups_total": len(groups),
            "groups_sent": 0,
            "groups_skipped_dedup": 0,
            "groups_failed": 0,
            "issues_sent": 0,
            "issues_skipped_dedup": 0,
        }

        if self.is_preview:
            logger.info(
                "PREVIEW MODE — all digests redirected to %s, dedup bypassed",
                self._preview_recipient.display_name,
            )

        for group in groups:
            if self._max_groups and stats["groups_sent"] >= self._max_groups:
                logger.info(
                    "Reached --max-digests limit (%d) — stopping early", self._max_groups
                )
                break
            if self.is_preview:
                # In preview: show all rules (both poc and live)
                self._deliver_group(group, run_date, stats)
            else:
                # In live mode: strip out any matches from poc-only rules
                live_matches = [m for m in group.matches if m.rule.status.lower() == "live"]
                if not live_matches:
                    stats["groups_skipped_dedup"] += 1
                    continue
                live_group = AlertGroup(owner=group.owner, owner_key=group.owner_key, matches=live_matches)
                fresh_group = self._drop_duplicates(live_group, stats)
                if not fresh_group.matches:
                    stats["groups_skipped_dedup"] += 1
                    logger.info("All issues already alerted for %s — skipping", group.display_name)
                    continue
                self._deliver_group(fresh_group, run_date, stats)

        logger.info("Dispatch complete: %s", stats)
        return stats

    # ------------------------------------------------------------------
    # Per-rule message cap
    # ------------------------------------------------------------------

    def _apply_per_rule_limit(self, groups: list[AlertGroup], limit: int) -> list[AlertGroup]:
        """Cap each rule to at most `limit` groups (messages). Extra matches are dropped."""
        rule_counts: dict[str, int] = {}
        result: list[AlertGroup] = []
        for group in groups:
            capped: list[RuleMatch] = []
            for match in group.matches:
                rid = match.rule.id
                if rule_counts.get(rid, 0) < limit:
                    capped.append(match)
                else:
                    logger.warning(
                        "Per-rule limit (%d) reached for rule '%s' — dropping match for %s",
                        limit, rid, group.display_name,
                    )
            if capped:
                result.append(AlertGroup(owner=group.owner, owner_key=group.owner_key, matches=capped))
                # Count once per rule per group (not per match)
                for rid in {m.rule.id for m in capped}:
                    rule_counts[rid] = rule_counts.get(rid, 0) + 1
        return result

    # ------------------------------------------------------------------
    # Sample-rules mode: one message per rule with a single example issue
    # ------------------------------------------------------------------

    def _build_sample_groups(self, groups: list[AlertGroup]) -> list[AlertGroup]:
        """Collapse all matches into one AlertGroup per rule (one issue each)."""
        seen_rules: dict[str, RuleMatch] = {}
        for group in groups:
            for match in group.matches:
                if match.rule.id not in seen_rules:
                    seen_rules[match.rule.id] = match

        sample_groups = []
        for rule_id, match in seen_rules.items():
            # Keep the real owner so the preview banner shows the correct recipient.
            # The normal preview redirect in _deliver_group handles actual delivery to you.
            real_owner = match.owner_override or match.owner
            ag = AlertGroup(
                owner=real_owner,
                owner_key=match.owner_key,
                matches=[match],
            )
            sample_groups.append(ag)
            logger.info("Sample rule '%s': using 1 example issue", rule_id)
        return sample_groups

    # ------------------------------------------------------------------
    # Per-group handling
    # ------------------------------------------------------------------

    def _drop_duplicates(self, group: AlertGroup, stats: dict) -> AlertGroup:
        fresh: list[RuleMatch] = []
        for match in group.matches:
            if self._dedup.is_duplicate(match.rule.id, match.issue.key):
                logger.debug("Dedup: skipping %s / %s", match.rule.id, match.issue.key)
                stats["issues_skipped_dedup"] += 1
            else:
                fresh.append(match)
        return AlertGroup(owner=group.owner, owner_key=group.owner_key, matches=fresh)

    def _deliver_group(
        self, group: AlertGroup, run_date: datetime, stats: dict
    ) -> None:
        # In preview mode, attach the reviewer's name so the banner can show it
        preview_label = self._preview_recipient.display_name if self.is_preview else None

        # When the sender can post interactive cards (Power Automate + snooze
        # flow), deliver the Adaptive Card so the ⏰ Snooze button stays inside
        # Teams. Otherwise fall back to the rich HTML digest.
        if getattr(self._sender, "supports_cards", False):
            payload = self._formatter.format_digest_card(
                group, run_date=run_date, preview_for=preview_label
            )
        else:
            payload = self._formatter.format_digest(
                group, run_date=run_date, preview_for=preview_label
            )

        if self._dry_run:
            self._log_dry_run(group)
            return

        # Determine the actual send target
        send_to_group = self._preview_group(group) if self.is_preview else group

        ok = self._send(payload, send_to_group)
        if ok:
            if not self.is_preview:
                # Only mark as alerted in a real run
                for match in group.matches:
                    self._dedup.mark_alerted(match.rule.id, match.issue.key)
            stats["groups_sent"] += 1
            stats["issues_sent"] += len(group.matches)
            if self.is_preview:
                logger.info(
                    "[PREVIEW] Sent preview of %s's digest (%d issue(s)) to %s",
                    group.display_name,
                    len(group.matches),
                    self._preview_recipient.display_name,
                )
            else:
                logger.info(
                    "Sent digest to %s — %d issue(s) across %d rule(s)",
                    group.display_name,
                    len(group.matches),
                    len({m.rule.id for m in group.matches}),
                )
        else:
            stats["groups_failed"] += 1
            logger.error(
                "Failed to deliver%s digest for %s",
                " preview of" if self.is_preview else "",
                group.display_name,
            )

    def _preview_group(self, group: AlertGroup) -> AlertGroup:
        """Return a shallow copy of the group with owner replaced by the preview recipient."""
        return AlertGroup(
            owner=self._preview_recipient,
            owner_key=self._preview_recipient.account_id,
            matches=group.matches,
        )

    # ------------------------------------------------------------------
    # Sender routing
    # ------------------------------------------------------------------

    def _send(self, payload: dict, group: AlertGroup) -> bool:
        """
        Power Automate  →  DM to group.owner.email via the flow's dynamic recipient.
        Graph API       →  DM to group.owner.account_id.
        Webhook         →  Post to channel (card title names the recipient).
        """
        if isinstance(self._sender, TeamsPowerAutomateSender):
            # In preview mode send_to_group.owner is the reviewer — use that.
            # Fall back to payload["recipient"] (the real owner) in normal mode.
            email = (group.owner.email if group.owner else None) or payload.get("recipient")
            if not email:
                logger.error(
                    "Cannot send Power Automate DM for '%s': no email in people.yaml",
                    group.display_name,
                )
                return False
            # A card payload → post via the snooze flow (no-browser Snooze button).
            if "card" in payload:
                return self._sender.send_card(payload, recipient_email=email)
            return self._sender.send(payload, recipient_email=email)

        if isinstance(self._sender, TeamsGraphSender):
            if group.owner:
                return self._sender.send_direct_message(group.owner.account_id, payload)
            return self._sender.send_to_channel(payload)

        if isinstance(self._sender, TeamsWebhookSender):
            return self._sender.send(payload)

        logger.error("Unknown sender type: %s", type(self._sender))
        return False

    # ------------------------------------------------------------------
    # Dry-run
    # ------------------------------------------------------------------

    def _log_dry_run(self, group: AlertGroup) -> None:
        rule_summary = ", ".join(
            f"{rid}({cnt})" for rid, cnt in self._count_by_rule(group).items()
        )
        target = (
            f"{self._preview_recipient.display_name} [preview of {group.display_name}]"
            if self.is_preview
            else group.display_name
        )
        logger.info(
            "[DRY RUN] Would send digest to %s — %d issue(s): %s",
            target, len(group.matches), rule_summary,
        )

    @staticmethod
    def _count_by_rule(group: AlertGroup) -> dict[str, int]:
        counts: dict[str, int] = {}
        for match in group.matches:
            counts[match.rule.id] = counts.get(match.rule.id, 0) + 1
        return counts
