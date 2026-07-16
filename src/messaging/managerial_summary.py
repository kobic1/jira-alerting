"""Managerial summary — a daily per-project rollup of live alerts for subscribers.

For each subscriber configured in settings.managerial_summary.subscribers_by_project,
build ONE HTML message covering every project they subscribe to, grouped by project
then by rule, listing each matching issue and the owner it was delivered to.

Read-only: this never reads or writes the deduplication store, so it always reflects
the current live state and never interferes with the real alert pipeline.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime

from src.config_loader import load_rules
from src.ingestion.people_registry import PeopleRegistry
from src.messaging.formatter import MessageFormatter, _SEVERITY_EMOJI, _SEVERITY_RANK
from src.rules.engine import RuleEngine

logger = logging.getLogger(__name__)

_SEV_RANKS = {"high": 3, "medium": 2, "low": 1}


def project_of(issue_key: str) -> str:
    """Project key is the prefix of the issue key (PMN-123 -> PMN)."""
    return issue_key.split("-")[0]


class ManagerialSummaryReporter:
    """Builds and sends the per-project managerial summary to its subscribers."""

    def __init__(
        self,
        engine: RuleEngine,
        formatter: MessageFormatter,
        sender,
        registry: PeopleRegistry,
        rules_path: str,
        subscribers_by_project: dict[str, list[str]],
        min_severity: str = "low",
        override_recipient: str | None = None,
        state_path: str = ".managerial_cache.json",
        force: bool = False,
    ):
        self._engine = engine
        self._formatter = formatter
        self._sender = sender
        self._registry = registry
        self._rules_path = rules_path
        self._subs_by_project = subscribers_by_project or {}
        self._min_rank = _SEV_RANKS.get(min_severity, 1)
        self._override = override_recipient  # send everything here instead (for testing)
        # Once-a-day guard: records the date each subscriber last received the
        # summary so a second run the same day skips them (prevents the duplicate
        # you get from manually re-triggering). Pass force=True to re-send anyway.
        # NOTE: on an ephemeral CI runner this file doesn't persist between runs,
        # so it guards local/shared-state re-runs; the scheduled once-a-day run is
        # never affected. Override sends (testing) bypass the guard and aren't
        # recorded.
        self._state_path = state_path
        self._force = force

    # ------------------------------------------------------------------

    def run(self) -> dict:
        subscribers = self._invert_subscribers()
        if not subscribers:
            logger.warning("Managerial summary: no subscribers configured — nothing to send")
            return {"subscribers": 0, "sent": 0, "failed": 0, "alerts_total": 0}

        by_project, rule_lookup, total = self._collect_matches()
        now = datetime.utcnow()
        date_str = now.strftime("%a %d %b %Y")
        today = now.strftime("%Y-%m-%d")

        # Guard applies only to real sends; testing overrides always go through.
        guarded = not self._override and not self._force
        state = self._load_state() if guarded else {}

        stats = {"subscribers": len(subscribers), "sent": 0, "failed": 0, "skipped": 0, "alerts_total": total}
        if self._override:
            logger.warning("Managerial summary: override active — all messages go to %s", self._override)
        if self._force:
            logger.warning("Managerial summary: --force active — bypassing the once-a-day guard")

        for email, (name, projects) in subscribers.items():
            if guarded and state.get(email) == today:
                stats["skipped"] += 1
                logger.info("Managerial summary already sent to %s today — skipping (use --force to re-send)", email)
                continue
            target = self._override or email
            html = self._build_html(name, projects, by_project, rule_lookup, date_str)
            ok = self._sender.send({"message": html}, recipient_email=target)
            if ok:
                stats["sent"] += 1
                logger.info("Managerial summary sent → %s (projects=%s)", target, projects)
                if guarded:
                    state[email] = today
                    self._save_state(state)
            else:
                stats["failed"] += 1
                logger.error("Managerial summary FAILED → %s", target)

        logger.info("Managerial summary complete: %s", stats)
        return stats

    # ------------------------------------------------------------------
    # Once-a-day state (separate from the alert dedup store)
    # ------------------------------------------------------------------

    def _load_state(self) -> dict[str, str]:
        if os.path.exists(self._state_path):
            try:
                with open(self._state_path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.warning("Could not read managerial cache; starting fresh")
        return {}

    def _save_state(self, state: dict[str, str]) -> None:
        try:
            with open(self._state_path, "w") as f:
                json.dump(state, f, indent=2)
        except OSError as exc:
            logger.warning("Could not write managerial cache: %s", exc)

    # ------------------------------------------------------------------
    # Data collection
    # ------------------------------------------------------------------

    def _active_rules(self) -> list:
        rules = load_rules(self._rules_path)
        return [
            r for r in rules
            if r.enabled
            and r.status.lower() == "live"
            and _SEV_RANKS.get(r.severity.value, 1) >= self._min_rank
        ]

    def _collect_matches(self):
        """Evaluate live rules and bucket unique matches by project -> rule_id -> [matches]."""
        groups = self._engine.evaluate_all(self._active_rules())
        seen: set[tuple[str, str]] = set()
        by_project: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        rule_lookup: dict[str, object] = {}
        for group in groups:
            for m in group.matches:
                dkey = (m.rule.id, m.issue.key)
                if dkey in seen:
                    continue
                seen.add(dkey)
                by_project[project_of(m.issue.key)][m.rule.id].append(m)
                rule_lookup[m.rule.id] = m.rule
        return by_project, rule_lookup, len(seen)

    def _invert_subscribers(self) -> dict[str, tuple[str, list[str]]]:
        """project->[emails] becomes email->(display_name, sorted[projects])."""
        by_email: dict[str, list[str]] = defaultdict(list)
        for project, emails in self._subs_by_project.items():
            for email in emails or []:
                if project not in by_email[email]:
                    by_email[email].append(project)

        result: dict[str, tuple[str, list[str]]] = {}
        for email, projects in by_email.items():
            person = self._registry.find_by_email(email)
            name = person.display_name if person else email.split("@")[0]
            result[email] = (name, sorted(projects))
        return result

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------

    def _build_html(self, name, projects, by_project, rule_lookup, date_str) -> str:
        sub_total = sum(
            len(ms) for p in projects for ms in by_project.get(p, {}).values()
        )
        parts: list[str] = [
            "<h2>📊 Daily JIRA Alert Summary — Management View</h2>",
            f"<p>Hi {name}, here is today's alert rollup &nbsp;·&nbsp; {date_str}</p>",
            f"<p><strong>{sub_total} alert(s)</strong> across {len(projects)} project(s): "
            f"{', '.join(projects)}</p>",
            "<hr/>",
        ]

        for proj in projects:
            rules_in = by_project.get(proj, {})
            proj_total = sum(len(ms) for ms in rules_in.values())
            parts.append(f"<h2>📁 {proj} — {proj_total} alert(s)</h2>")

            if not rules_in:
                parts.append("<p>✅ No live alerts today.</p><hr/>")
                continue

            ordered = sorted(
                rules_in.items(),
                key=lambda kv: (
                    rule_lookup[kv[0]].priority,
                    _SEVERITY_RANK[rule_lookup[kv[0]].severity],
                    rule_lookup[kv[0]].name,
                ),
            )
            for rid, matches in ordered:
                rule = rule_lookup[rid]
                emoji = _SEVERITY_EMOJI[rule.severity]
                rows = []
                for m in matches:
                    detail = self._formatter._render_template(rule.message_template, m).strip()
                    owner = m.owner.display_name if m.owner else "Unassigned"
                    rows.append(
                        f'<li><a href="{m.issue.url}"><strong>{m.issue.key}</strong></a> — {m.issue.summary}<br/>'
                        f'<small>👤 {owner} &nbsp;·&nbsp; {detail}</small></li>'
                    )
                parts.append(
                    f"<h3>{emoji} {rule.name} &nbsp;<small>({len(matches)})</small></h3>"
                    f"<ul>{''.join(rows)}</ul>"
                )
            parts.append("<hr/>")

        return "\n".join(parts)
