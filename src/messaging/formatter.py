"""Message generation — builds a daily digest per person.

Two output formats from the same data:
  format_digest()  → HTML string  (for Teams DM via Power Automate flow)
  format_card()    → Adaptive Card JSON  (for Graph API / channel webhooks)

HTML renders natively in a Teams chat message — bold, links, and structure
all appear exactly as in any Teams DM. No card schema needed.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import quote

from src.ingestion.jira_client import JiraClient
from src.models import AlertGroup, RuleConfig, RuleMatch, Severity

_SEVERITY_EMOJI = {
    Severity.HIGH:   "🔴",
    Severity.MEDIUM: "🟡",
    Severity.LOW:    "🔵",
}

_SEVERITY_RANK = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


def _strip_html(text: str) -> str:
    """Flatten HTML to plain text — Adaptive Card TextBlocks don't render HTML."""
    return re.sub(r"<[^>]+>", "", text).replace("&nbsp;", " ").strip()


def _html_to_card_md(text: str) -> str:
    """HTML → Adaptive-Card markdown: <b>/<strong> become **bold**, other tags drop.

    Adaptive Card TextBlocks render a markdown subset (bold, links) but NOT HTML
    and NOT inline color — color is applied per-TextBlock by the caller.
    """
    text = re.sub(r"</?(?:strong|b)>", "**", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&nbsp;", " ").strip()


class MessageFormatter:
    def __init__(self, jira_client: JiraClient, snooze_flow_url: str | None = None):
        self._jira = jira_client
        # When set, every issue row gets a "⏰ Snooze 2h" link pointing at a
        # Power Automate flow that waits 2 hours and then re-posts a reminder.
        # Left as None (feature off) if SNOOZE_FLOW_URL isn't configured.
        self._snooze_flow_url = snooze_flow_url

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def format_digest(
        self,
        group: AlertGroup,
        run_date: datetime | None = None,
        preview_for: str | None = None,
    ) -> dict[str, Any]:
        """Return a payload dict with both 'message' (HTML) and 'recipient' fields.

        The Power Automate flow receives this and routes the HTML message as a
        Teams DM to the right person.

        preview_for: when set, prepends a visible PREVIEW banner so the reviewer
                     knows who would normally receive this message.
        """
        date_str = (run_date or datetime.utcnow()).strftime("%a %d %b %Y")
        total = len(group.matches)
        noun = "item" if total == 1 else "items"
        by_rule = self._group_by_rule(group.matches)

        parts: list[str] = []

        if preview_for:
            parts.append(self._preview_banner_html(group.display_name, preview_for))

        parts.append(
            f"<h2>📋 Daily JIRA Signals — {group.display_name}</h2>"
            f"<p><strong>{total} {noun}</strong> need your attention &nbsp;·&nbsp; {date_str}</p>"
            f"<hr/>"
        )

        recipient_email = group.owner.email if group.owner else None

        for rule, matches in by_rule:
            parts.append(self._rule_section_html(rule, matches, recipient_email))

        html = "\n".join(parts)

        return {
            "recipient": recipient_email,
            "message":   html,
        }

    def format_digest_card(
        self,
        group: AlertGroup,
        run_date: datetime | None = None,
    ) -> dict[str, Any]:
        """Return {recipient, card} — an Adaptive Card version of the digest.

        The ⏰ Snooze button is an ``Action.Submit`` (not a URL), so clicking it
        opens no browser. It's delivered by a flow that "posts an adaptive card
        and waits for a response"; the submitted ``data.action == 'snooze'``
        tells that flow to Delay 2h and re-post the reminder — all inside Teams.
        """
        date_str = (run_date or datetime.utcnow()).strftime("%a %d %b %Y")
        total = len(group.matches)
        noun = "item" if total == 1 else "items"
        recipient_email = group.owner.email if group.owner else None

        body: list[dict[str, Any]] = [
            {"type": "TextBlock", "size": "Large", "weight": "Bolder", "wrap": True,
             "text": f"📋 Daily JIRA Signals — {group.display_name}"},
            {"type": "TextBlock", "isSubtle": True, "spacing": "None", "wrap": True,
             "text": f"{total} {noun} need your attention · {date_str}"},
        ]
        for rule, matches in self._group_by_rule(group.matches):
            emoji = _SEVERITY_EMOJI[rule.severity]
            filter_url = self._jira.get_filter_url(rule.jira_filter_id, rule.jql)
            body.append(
                {"type": "TextBlock", "weight": "Bolder", "size": "Medium", "separator": True,
                 "wrap": True, "text": f"{emoji} {rule.name} ({len(matches)})"}
            )
            if rule.description:
                body.append(
                    {"type": "TextBlock", "isSubtle": True, "spacing": "None", "wrap": True,
                     "text": f"_{rule.description}_"}
                )
            for m in matches[: self._MAX_ISSUES_PER_RULE]:
                detail = _html_to_card_md(self._render_template(m.rule.message_template, m).strip())
                # Bullet in a narrow column + issue/detail stacked in the wide column,
                # so the detail indents under the issue (like the reminder's <li>).
                # Black text with bold accents (from <strong>) — Adaptive Cards can't
                # color individual words the way the HTML reminder does.
                content = [
                    {"type": "TextBlock", "wrap": True,
                     "text": f"[{m.issue.key}]({m.issue.url}) — {m.issue.summary}"}
                ]
                if detail:
                    content.append(
                        {"type": "TextBlock", "size": "Small", "spacing": "None",
                         "wrap": True, "text": detail}
                    )
                body.append({
                    "type": "ColumnSet", "spacing": "Small",
                    "columns": [
                        {"type": "Column", "width": "auto",
                         "items": [{"type": "TextBlock", "text": "•"}]},
                        {"type": "Column", "width": "stretch", "items": content},
                    ],
                })
            body.append(
                {"type": "TextBlock", "spacing": "Small", "wrap": True,
                 "text": f"[🔗 View all in Jira]({filter_url})"}
            )

        card = {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
            "msteams": {"width": "Full"},  # full-width card, matching the reminder message
            "body": body,
            "actions": [
                {"type": "Action.Submit", "title": "⏰ Snooze 2h",
                 "data": {"action": "snooze", "recipient": recipient_email}},
            ],
        }
        return {
            "recipient": recipient_email,
            "card":      card,
        }

    # ------------------------------------------------------------------
    # HTML building blocks
    # ------------------------------------------------------------------

    def _preview_banner_html(self, real_recipient: str, reviewer: str) -> str:
        return (
            f'<blockquote style="border-left:4px solid #f1a12b; padding:8px; margin:0 0 12px 0;">'
            f'<strong>👁️ PREVIEW — not yet sent</strong><br/>'
            f'Real recipient: <strong>{real_recipient}</strong><br/>'
            f'Reviewing as: {reviewer}<br/>'
            f'<em>Run <code>python3 main.py --run-once</code> to approve and send for real.</em>'
            f'</blockquote>'
        )

    _MAX_ISSUES_PER_RULE = 5

    def _rule_section_html(
        self, rule: RuleConfig, matches: list[RuleMatch], recipient_email: str | None = None
    ) -> str:
        emoji = _SEVERITY_EMOJI[rule.severity]
        count = len(matches)
        filter_url = self._jira.get_filter_url(rule.jira_filter_id, rule.jql)

        shown = matches[: self._MAX_ISSUES_PER_RULE]
        overflow = count - len(shown)

        rows = "\n".join(self._issue_row_html(m, recipient_email) for m in shown)
        overflow_line = (
            f'<li><em>…and <a href="{filter_url}">{overflow} more</a> — view all in Jira</em></li>'
            if overflow > 0
            else ""
        )

        return (
            f"<h3>{emoji} {rule.name} &nbsp;<small>({count})</small></h3>"
            f"<p><em>{rule.description}</em></p>"
            f"<ul>{rows}{overflow_line}</ul>"
            f'<p><a href="{filter_url}">🔗 View all in Jira</a></p>'
            f"<hr/>"
        )

    def _issue_row_html(self, match: RuleMatch, recipient_email: str | None = None) -> str:
        issue = match.issue
        detail = self._render_template(match.rule.message_template, match).strip()
        snooze = self._snooze_link_html(issue, recipient_email)
        return (
            f'<li>'
            f'<a href="{issue.url}"><strong>{issue.key}</strong></a> — {issue.summary}<br/>'
            f'<small>{detail}{snooze}</small>'
            f'</li>'
        )

    def _snooze_link_html(self, issue, recipient_email: str | None) -> str:
        """A '⏰ Snooze 2h' hyperlink, or '' when snooze isn't configured.

        The snooze flow is identical to the alert flow — after a 2h delay it
        posts a `message` to a `recipient`. Those are the only two fields it
        needs, so the link carries both. A clicked link is a GET, so they ride
        in the query string; the flow reads them from
        triggerOutputs()?['queries'] rather than the request body.
        """
        if not self._snooze_flow_url or not recipient_email:
            return ""
        reminder = (
            "⏰ <b>Snoozed reminder</b><br/>"
            f'<a href="{issue.url}"><b>{issue.key}</b></a> — {issue.summary}<br/>'
            "<small>You snoozed this earlier — resurfacing it now.</small>"
        )
        params = (
            f"&recipient={quote(recipient_email)}"
            f"&message={quote(reminder)}"
        )
        href = f"{self._snooze_flow_url}{params}"
        return (
            f' &nbsp;·&nbsp; '
            f'<a href="{href}" title="Remind me about this issue again in 2 hours">⏰ Snooze 2h</a>'
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _group_by_rule(
        self, matches: list[RuleMatch]
    ) -> list[tuple[RuleConfig, list[RuleMatch]]]:
        bucket: dict[str, list[RuleMatch]] = defaultdict(list)
        rules: dict[str, RuleConfig] = {}
        for match in matches:
            bucket[match.rule.id].append(match)
            rules[match.rule.id] = match.rule
        return sorted(
            ((rules[rid], ms) for rid, ms in bucket.items()),
            key=lambda pair: (pair[0].priority, _SEVERITY_RANK[pair[0].severity], pair[0].name),
        )

    def format_manager_digest(
        self,
        groups: list[AlertGroup],
        run_date: datetime | None = None,
        manager_name: str = "Manager",
        projects: list[str] | None = None,
    ) -> dict[str, Any]:
        """Single consolidated card for a manager showing all alerts grouped by rule."""
        date_str = (run_date or datetime.utcnow()).strftime("%a %d %b %Y")
        all_matches = [m for g in groups for m in g.matches]
        total = len(all_matches)
        noun = "item" if total == 1 else "items"
        projects_str = " · ".join(projects) if projects else "All projects"

        by_rule: dict[str, list[RuleMatch]] = defaultdict(list)
        rules: dict[str, RuleConfig] = {}
        for match in all_matches:
            by_rule[match.rule.id].append(match)
            rules[match.rule.id] = match.rule

        sorted_rules = sorted(
            ((rules[rid], ms) for rid, ms in by_rule.items()),
            key=lambda pair: (pair[0].priority, _SEVERITY_RANK[pair[0].severity]),
        )

        parts: list[str] = [
            f"<h2>📊 Manager Alert Review — {projects_str}</h2>"
            f"<p><strong>{total} {noun}</strong> triggered across {len(sorted_rules)} rule(s) &nbsp;·&nbsp; {date_str}</p>"
            f"<hr/>"
        ]

        for rule, matches in sorted_rules:
            emoji = _SEVERITY_EMOJI[rule.severity]
            count = len(matches)
            filter_url = self._jira.get_filter_url(rule.jira_filter_id, rule.jql)
            rows = []
            for m in matches:
                issue = m.issue
                owner_name = (m.owner_override.display_name if m.owner_override else None) or \
                             (issue.assignee.display_name if issue.assignee else "Unassigned")
                detail = self._render_template(rule.message_template, m).strip()
                rows.append(
                    f'<li>'
                    f'<a href="{issue.url}"><strong>{issue.key}</strong></a> — {issue.summary}'
                    f' &nbsp;<em>({owner_name})</em><br/>'
                    f'<small>{detail}</small>'
                    f'</li>'
                )
            parts.append(
                f"<h3>{emoji} {rule.name} &nbsp;<small>({count})</small></h3>"
                f"<ul>{''.join(rows)}</ul>"
                f'<p><a href="{filter_url}">🔗 View all in Jira</a></p>'
                f"<hr/>"
            )

        return {
            "recipient": None,
            "message": "\n".join(parts),
        }

    def _render_template(self, template: str, match: RuleMatch) -> str:
        try:
            return template.format(**match.context)
        except KeyError:
            return template
