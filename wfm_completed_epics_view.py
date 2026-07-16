"""Template: "WFM Organization — Completed Epics Awaiting Closure".

A single-alert management view — the "Completed Epic, Status Not Done" rule
(epic in Validation, ALL child issues Done, unchanged 2+ business days),
consolidated across the WFM-org projects and grouped by project. Delivered as a
Teams DM via the same Power Automate flow the alerts use.

Usage:
    python3 wfm_completed_epics_view.py [recipient_email]

`recipient_email` defaults to Kobi. Whoever is named receives the EXACT view
Kobi receives (real data, same format). Run from the repo with `.env` sourced.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime
import html as _html

from src.config_loader import load_settings, load_rules, load_people
from src.ingestion.jira_client import JiraClient
from src.rules.engine import RuleEngine
import main

DEFAULT_RECIPIENT = "kobi.cohen@nice.com"
WFM_PROJECTS = ["PMN", "WFM", "CXINT", "EEM", "WFMDEVOPS", "CXCO", "CXSUP"]


def build_message() -> tuple[str, int, int]:
    s = load_settings("config/settings.yaml"); j = s["jira"]
    jc = JiraClient(base_url=j["base_url"], email=j["email"], api_token=j["api_token"], timeout=30)
    rule = next(r for r in load_rules("config/rules.yaml") if r.id == "epic_complete_not_done")
    eng = RuleEngine(jira_client=jc, people_registry=load_people("config/people.yaml"),
                     project_filter=WFM_PROJECTS, issue_type_filter=["Epic"], max_issues_per_rule=500)
    matches = [m for g in eng.evaluate_all([rule]) for m in g.matches]

    by_proj: dict[str, list] = {}
    for m in matches:
        by_proj.setdefault(m.issue.key.split("-")[0], []).append(m)

    date_str = datetime.utcnow().strftime("%a %d %b %Y")
    total = len(matches)
    parts = [
        "<h2>🟠 WFM Organization — Completed Epics Awaiting Closure</h2>",
        f"<p><strong>{total} epic(s)</strong> completed but still in <em>Validation</em> "
        f"&nbsp;·&nbsp; {date_str}</p>",
        f"<p><small>Alert: <strong>Completed Epic, Status Not Done</strong> · "
        f"Scope: {', '.join(WFM_PROJECTS)}</small></p>",
        "<hr/>",
    ]
    if not matches:
        parts.append("<p>✅ No completed-but-not-Done epics across the WFM projects today.</p>")
    else:
        for proj in sorted(by_proj):
            rows = []
            for m in by_proj[proj]:
                c = m.context
                lead = m.owner.display_name if m.owner else "—"
                rows.append(
                    f'<li><a href="{m.issue.url}"><strong>{m.issue.key}</strong></a> — '
                    f'{_html.escape(m.issue.summary)}<br/>'
                    f'<small>👤 Project lead: {_html.escape(lead)} · '
                    f'✅ {c["children_done"]}/{c["children_total"]} children Done · '
                    f'no change {c["business_days_since_update"]} business day(s)</small></li>'
                )
            parts.append(f"<h3>📁 {proj} &nbsp;<small>({len(by_proj[proj])})</small></h3><ul>{''.join(rows)}</ul>")
        clear = [p for p in WFM_PROJECTS if p not in by_proj]
        if clear:
            parts.append(f"<hr/><p><small>No completed-epic alerts in: {', '.join(clear)}</small></p>")

    return "".join(parts), total, len(by_proj)


def main_send(recipient: str) -> None:
    message, total, nproj = build_message()
    sender = main.build_sender(load_settings("config/settings.yaml")["teams"])
    ok = sender.send({"message": message}, recipient_email=recipient)
    print(f"'WFM Organization — Completed Epics Awaiting Closure' -> {recipient}: "
          f"{'sent ✓' if ok else 'FAILED ✗'} ({total} epic(s) across {nproj} project(s))")


if __name__ == "__main__":
    main_send(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RECIPIENT)
