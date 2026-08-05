#!/usr/bin/env python3
"""PMN release-status KPI dashboard — unattended build + email, for GitHub Actions.

Rebuilds the same dashboard the interactive `pmn-kpi-dashboard` skill produces
(6 stat cards + 5 charts, one self-contained HTML) straight from the Jira REST
API, verifies it, and emails it through Kobi's Power Automate flow.

    python3 pmn_dashboard/build_and_send.py --audience managers
    python3 pmn_dashboard/build_and_send.py --audience test          # Kobi only
    python3 pmn_dashboard/build_and_send.py --audience none          # build, don't send

Environment (GitHub Actions secrets):
    JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN   — Jira Cloud basic auth
    POWER_AUTOMATE_EMAIL_URL                    — the "Send an email V2" flow trigger

WHAT THIS CAN AND CANNOT REFRESH
    Live from Jira every run : Delivery, Quality, % Epics by AI, AI Fields Adoption
    NOT refreshable here     : Epic Dev Cycle Time (EDCT)

EDCT lives in the "R&D Efficiency" Power BI app, behind interactive Microsoft
SSO. There is no headless route -- the Power BI MCP connector does not expose
that dataset (checked 2026-08-05: only 11 certified finance/HR datasets), and
the report is client-side rendered, so nothing can scrape it from CI. So EDCT
is read from `powerbi_snapshot.json`, a committed snapshot refreshed by an
interactive session, and the chart caption always states its as-of date. EDCT
is a monthly average, so a snapshot that is a week or two old is still a fair
number -- it just must never be presented as today's.

That same snapshot is what the verification gate reconciles the Jira-derived AI
numbers against, so a definition drift in a CLOSED month fails the build.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HERE = Path(__file__).resolve().parent
PROJECT = "PMN"

# Field IDs (per-instance, stable across releases -- see the skill's jira_field_ids.md)
CF_IMPLEMENTED_BY_AI = "cf[15229]"   # "Implemented by AI Agent" (Yes/No)
CF_PR_URL = "cf[15262]"              # PR URL -- the "new AI metrics" signal

MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# --------------------------------------------------------------------------- Jira

class Jira:
    def __init__(self) -> None:
        base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
        email = os.environ.get("JIRA_EMAIL", "")
        token = os.environ.get("JIRA_API_TOKEN", "")
        missing = [k for k, v in (("JIRA_BASE_URL", base), ("JIRA_EMAIL", email),
                                 ("JIRA_API_TOKEN", token)) if not v]
        if missing:
            sys.exit(f"FATAL: missing environment: {', '.join(missing)}")
        self.base = base
        self.s = requests.Session()
        self.s.auth = (email, token)
        self.s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        retry = Retry(total=4, backoff_factor=1.5,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET", "POST"])
        self.s.mount("https://", HTTPAdapter(max_retries=retry))

    def count(self, jql: str) -> int:
        """Issue count. Uses the approximate-count endpoint (the only cheap count on
        Jira Cloud v3); falls back to paging if it is unavailable."""
        r = self.s.post(f"{self.base}/rest/api/3/search/approximate-count",
                        json={"jql": jql}, timeout=60)
        if r.status_code == 200:
            return int(r.json()["count"])
        if r.status_code in (404, 410):
            return len(self.search(jql, ["key"]))
        r.raise_for_status()
        raise RuntimeError("unreachable")

    def search(self, jql: str, fields: list[str]) -> list[dict]:
        """All issues matching jql, following nextPageToken to the end."""
        out: list[dict] = []
        token = None
        while True:
            body = {"jql": jql, "fields": fields, "maxResults": 100}
            if token:
                body["nextPageToken"] = token
            r = self.s.post(f"{self.base}/rest/api/3/search/jql", json=body, timeout=90)
            r.raise_for_status()
            data = r.json()
            out.extend(data.get("issues", []))
            token = data.get("nextPageToken")
            if data.get("isLast", True) or not token:
                return out


def month_bounds(year: int, month: int) -> tuple[str, str]:
    start = dt.date(year, month, 1)
    nxt = dt.date(year + (month == 12), (month % 12) + 1, 1)
    return start.isoformat(), nxt.isoformat()


def d(s: str | None) -> dt.date | None:
    return dt.date(int(s[0:4]), int(s[5:7]), int(s[8:10])) if s else None


# ------------------------------------------------------------------- data sections

def build_delivery(j: Jira, cfg: dict, today: dt.date) -> dict:
    rel = cfg["release"]
    start = d(cfg["release_start_date"])
    issues = j.search(
        f'project = {PROJECT} AND fixVersion = "{rel}" AND issuetype = Epic AND status = Done',
        ["resolutiondate"])
    days = []
    for i in issues:
        rd = d((i.get("fields") or {}).get("resolutiondate"))
        if rd:
            days.append(max(0, (rd - start).days))
    days.sort()
    return {
        "release_history": cfg["release_history"],
        "release_order": cfg["release_order"],
        "current_delivered": len(days),
        "release_start_date": cfg["release_start_date"],
        "release_end_date": cfg["release_end_date"],
        "sprint_boundaries": cfg["sprint_boundaries"],
        "days_into_release_delivered": days,
        "forecast_override": cfg.get("forecast_override"),
        "as_of": today.isoformat(),
    }


def build_quality(j: Jira, today: dt.date, weeks_back: int = 9) -> dict:
    """Weekly created / resolved / running-open for the full bug backlog, excluding
    Accessibility. The `resolution is EMPTY` clause is what makes the opening
    baseline trustworthy -- without it, long-open bugs created before the window
    are missed and the Total Open line starts too low."""
    this_monday = today - dt.timedelta(days=today.weekday())
    weeks = [this_monday - dt.timedelta(days=7 * i) for i in range(weeks_back - 1, -1, -1)]
    ws = weeks[0]
    jql = (f'project = {PROJECT} AND issuetype = Bug AND summary !~ "\\"[Accessibility]*\\"" '
           f'AND (created >= "{ws}" OR resolutiondate >= "{ws}" OR resolution is EMPTY)')
    issues = j.search(jql, ["created", "resolutiondate"])
    recs = [(d(i["fields"].get("created")), d(i["fields"].get("resolutiondate")))
            for i in issues]

    baseline = sum(1 for c, r in recs if c and c < ws and (r is None or r >= ws))
    created, resolved, open_trend = [], [], []
    run = baseline
    for w in weeks:
        e = w + dt.timedelta(days=7)
        c = sum(1 for cc, _ in recs if cc and w <= cc < e)
        rr = sum(1 for _, r in recs if r and w <= r < e)
        run += c - rr
        created.append(c)
        resolved.append(rr)
        open_trend.append(run)
    return {
        "weeks": [w.strftime("%d%b") for w in weeks],
        "created": created, "resolved": resolved, "open_trend": open_trend,
        "as_of": today.isoformat(),
    }


def build_ai_epics(j: Jira, today: dt.date) -> dict:
    """Reproduces the Power BI '% AI Usage Trend' chart. AI epics are the
    cf[15229] Implemented-by-AI-Agent FIELD, not the AGENTIC_AI_CODE label --
    the label undercounts (July 2026: 20 by label vs 23 by field, and the
    report says 23)."""
    months, totals, ais = [], [], []
    for m in range(1, today.month + 1):
        lo, hi = month_bounds(today.year, m)
        window = f'AND resolutiondate >= "{lo}" AND resolutiondate < "{hi}"'
        base = f'project = {PROJECT} AND issuetype = Epic AND status = Done {window}'
        months.append(MONTHS_SHORT[m - 1] + ("*" if m == today.month else ""))
        totals.append(j.count(base))
        ais.append(j.count(f'{base} AND {CF_IMPLEMENTED_BY_AI} = "Yes"'))
    return {"_definition": "Total = epics Done resolved in month; AI = same with "
                           "cf[15229] Implemented by AI Agent = Yes (the report's definition).",
            "months": months, "total_epics": totals, "ai_epics": ais,
            "as_of": today.isoformat()}


def build_ai_fields(j: Jira, today: dt.date, first_month: int = 4) -> dict:
    """Marked-as-AI vs having-the-PR-URL-metric, monthly. Scope matches the Power BI
    'AI Fields Adaption' page filters: ALL issue types, ALL statuses, by resolved
    date -- so the metrics series sums to the report's quarterly '# Issues with PR
    ID'. Narrowing this to Done Stories/Bugs understates it badly."""
    months, marked, metrics = [], [], []
    for m in range(first_month, today.month + 1):
        lo, hi = month_bounds(today.year, m)
        window = f'resolutiondate >= "{lo}" AND resolutiondate < "{hi}"'
        months.append(calendar.month_name[m] + ("*" if m == today.month else ""))
        marked.append(j.count(f'project = {PROJECT} AND {CF_IMPLEMENTED_BY_AI} = "Yes" AND {window}'))
        metrics.append(j.count(f'project = {PROJECT} AND {CF_PR_URL} is not EMPTY AND {window}'))
    return {"_definition": "Marked as AI = cf[15229] = Yes; having metrics = cf[15262] PR-URL "
                           "populated. All issue types, all statuses, by resolved date.",
            "months": months, "marked_ai": marked, "new_metrics": metrics,
            "as_of": today.isoformat()}


# ------------------------------------------------------------------------ captions

def pct(a: int, b: int) -> float:
    return 100.0 * a / b if b else 0.0


def build_meta(data: dict, cfg: dict, snapshot: dict, today: dt.date) -> dict:
    dl, ql = data["delivery"], data["quality"]
    ae, af = data["ai_epics_pct"], data["ai_fields_adoption"]
    ed = data.get("edct")

    day_of_release = (today - d(cfg["release_start_date"])).days
    forecast = dl.get("forecast_override") or 0
    last_close = d(cfg["release_start_date"]) + dt.timedelta(days=max(dl["days_into_release_delivered"] or [0]))

    # AI headline figures use the last CLOSED month -- a 3-day-old month is not a rate.
    closed = -2 if len(ae["months"]) > 1 else -1
    ae_pct_closed = pct(ae["ai_epics"][closed], ae["total_epics"][closed])
    af_closed = -2 if len(af["months"]) > 1 else -1
    af_pct_closed = pct(af["new_metrics"][af_closed], af["marked_ai"][af_closed])
    ae_cur = pct(ae["ai_epics"][-1], ae["total_epics"][-1])
    af_cur = pct(af["new_metrics"][-1], af["marked_ai"][-1])

    peak = max(ql["open_trend"])
    peak_week = ql["weeks"][ql["open_trend"].index(peak)]
    edct_stale = ed and ed.get("as_of") != today.isoformat()

    stats = [
        {"num": str(dl["current_delivered"]), "lbl": "Epics delivered"},
        {"num": str(forecast), "lbl": "Forecast epics"},
        {"num": str(ed["values"][closed]) if ed else "—", "lbl": "EDCT days"},
        {"num": str(ql["open_trend"][-1]), "lbl": "Open bugs"},
        {"num": f"{af_pct_closed:.1f}%", "lbl": "AI field adoption"},
        {"num": f"{ae_pct_closed:.1f}%", "lbl": "Epics by AI"},
    ]

    sections = [{
        "file": "chart_delivery.png", "rail": "#1F3A5F", "tint": "rgba(31,58,95,0.06)",
        "seclabel": "Delivery", "title": "Epic Delivery &mdash; Actual vs. Expected",
        "caption": (f"{dl['current_delivered']} delivered on day {day_of_release} of the release, "
                    f"forecast {forecast} (capacity-adjusted for the departed teams). "
                    f"Last epic closed {last_close.strftime('%b %-d')}. Live from PMN Jira."),
    }]
    if ed:
        sections.append({
            "file": "chart_edct.png", "rail": "#C77700", "tint": "rgba(199,119,0,0.06)",
            "seclabel": "Efficiency", "title": "Epic Dev Cycle Time (EDCT)",
            "caption": (f"Monthly avg cycle time, Implemented-by-AI-Agent view "
                        f"(target &le;{ed.get('target_all', 10)} days). "
                        f"{ed['months'][closed]} closed at {ed['values'][closed]} days. "
                        + (f"<b>Carried forward &mdash; as of {ed['as_of']}.</b> EDCT comes from the "
                           f"R&amp;D Efficiency Power BI report, which needs an interactive "
                           f"sign-in and cannot be refreshed by this automated run; every other "
                           f"section below is live. "
                           if edct_stale else "Pulled live from R&amp;D Efficiency Power BI. ")
                        + "Source: R&amp;D Efficiency Power BI, Epic Dev Cycle Time."),
        })
    sections.append({
        "file": "chart_quality.png", "rail": "#2E9E4F", "tint": "rgba(46,158,79,0.06)",
        "seclabel": "Quality", "title": "Open Bugs Trend &mdash; Full Backlog",
        "caption": (f"{ql['open_trend'][-1]} open, full-project backlog (excl. Accessibility) "
                    f"&mdash; against a {peak_week} peak of {peak}. The week of {ql['weeks'][-1]} "
                    f"is still running ({ql['created'][-1]} opened, {ql['resolved'][-1]} closed). "
                    f"Live from PMN Jira."),
    })
    sections.append({
        "file": "chart_ai_epics_pct.png", "rail": "#7B4FC7", "tint": "rgba(123,79,199,0.06)",
        "seclabel": "AI Adoption", "title": "% Epics Developed by AI Agents",
        "caption": (f"{ae_pct_closed:.0f}% of epics AI-developed in {ae['months'][closed]} "
                    f"({ae['ai_epics'][closed]} of {ae['total_epics'][closed]}); "
                    f"{ae['months'][-1]} stands at {ae['ai_epics'][-1]} of {ae['total_epics'][-1]} "
                    f"({ae_cur:.0f}%) month-to-date. Counted the way the report counts it &mdash; "
                    f"the Implemented-by-AI-Agent field, not the AGENTIC_AI_CODE label. "
                    f"Live from PMN Jira."),
    })
    sections.append({
        "file": "chart_ai_fields_adoption.png", "rail": "#7B4FC7", "tint": "rgba(123,79,199,0.06)",
        "seclabel": "AI Adoption", "title": "AI Fields Adoption (All Issue Types)",
        "caption": (f"{af_pct_closed:.1f}% of AI-marked issues carry the PR-URL metric field in "
                    f"{af['months'][af_closed]} ({af['new_metrics'][af_closed]} of "
                    f"{af['marked_ai'][af_closed]}); {af_cur:.1f}% in {af['months'][-1]} so far "
                    f"({af['new_metrics'][-1]} of {af['marked_ai'][-1]}). Scope matches the "
                    f"report's AI Fields Adaption page &mdash; all issue types and statuses by "
                    f"resolved date. Live from PMN Jira."),
    })

    return {
        "release": cfg["release"], "project": PROJECT,
        "as_of_label": today.strftime("%b %-d %Y"),
        "stats": stats, "sections": sections,
        "footer": (f"Built from PMN Jira by a scheduled GitHub Actions run on "
                   f"{today.strftime('%b %-d %Y')}, and verified before sending: per-section "
                   f"freshness, the current month present in every series, and every AI/EDCT "
                   f"figure reconciled against the R&amp;D Efficiency Power BI report "
                   f"(snapshot {snapshot.get('last_synced', 'n/a')}). Delivery, Quality and both "
                   f"AI sections are live Jira; EDCT is the Power BI snapshot, dated in its "
                   f"caption above."),
    }


# ---------------------------------------------------------------------------- main

def run(cmd: list[str], label: str) -> None:
    print(f"\n$ {' '.join(str(c) for c in cmd)}", flush=True)
    r = subprocess.run([str(c) for c in cmd])
    if r.returncode != 0:
        sys.exit(f"FATAL: {label} failed (exit {r.returncode}) -- nothing was sent.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audience", choices=["managers", "test", "none"], default="test",
                    help="'managers' = the full PMN distribution list from config.json; "
                         "'test' = Kobi only; 'none' = build and verify, send nothing.")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--today", default=None, help="override today's date (YYYY-MM-DD)")
    args = ap.parse_args()

    cfg = json.load(open(HERE / "config.json"))
    snapshot = json.load(open(HERE / "powerbi_snapshot.json"))
    today = d(args.today) if args.today else dt.date.today()
    out = Path(args.outdir or (HERE / "build"))
    (out / "charts").mkdir(parents=True, exist_ok=True)

    start = cfg.get("first_send_date")
    if start and today < d(start) and args.audience == "managers":
        print(f"Not sending to managers before {start} (today is {today}). "
              f"Scheduled start date not reached -- exiting cleanly.")
        return 0

    print(f"PMN dashboard build — {today}  (audience: {args.audience})")
    j = Jira()

    data = {
        "release": cfg["release"],
        "delivery": build_delivery(j, cfg, today),
        "quality": build_quality(j, today),
        "ai_epics_pct": build_ai_epics(j, today),
        "ai_fields_adoption": build_ai_fields(j, today),
    }

    # EDCT: snapshot only. Flag the current month as a deliberate omission when the
    # snapshot predates it, so the verifier warns instead of failing the build.
    edct = dict(snapshot["edct_series"])
    if not any(str(m).rstrip("*").endswith(f"/{today.month:02d}") for m in edct["months"]):
        edct["omit_current_month"] = (f"EDCT is a Power BI figure requiring interactive sign-in; "
                                      f"snapshot is from {edct.get('as_of')}")
    data["edct"] = edct

    report_values = {k: v for k, v in snapshot.items() if k != "edct_series"}

    data_path, report_path = out / "data.json", out / "report_values.json"
    json.dump(data, open(data_path, "w"), indent=2)
    json.dump(report_values, open(report_path, "w"), indent=2)

    # ---- the gate: never email an unverified dashboard
    run([sys.executable, HERE / "verify_data.py", "--data", data_path,
         "--report", report_path, "--today", today.isoformat(), "--snapshot-stale-ok"],
        "data verification")

    run([sys.executable, HERE / "build_charts.py", "--data", data_path,
         "--outdir", out / "charts"], "chart build")

    meta_path = out / "meta.json"
    json.dump(build_meta(data, cfg, snapshot, today), open(meta_path, "w"), indent=2)
    html = out / f"PMN {cfg['release']} Release Dashboard.html"
    run([sys.executable, HERE / "assemble_dashboard_html.py", "--charts-dir", out / "charts",
         "--meta", meta_path, "--out", html], "HTML assembly")

    if args.audience == "none":
        print(f"\nBuilt {html} — no send requested.")
        return 0

    # Addresses come from secrets, never from the (public) repo.
    if args.audience == "managers":
        raw = os.environ.get("PMN_DASHBOARD_RECIPIENTS", "")
        if not raw.strip():
            sys.exit("FATAL: PMN_DASHBOARD_RECIPIENTS is not set — refusing to guess the "
                     "distribution list. Nothing was sent.")
    else:
        raw = os.environ.get("PMN_DASHBOARD_TEST_RECIPIENT", "kobi.cohen@nice.com")
    recipients = [a.strip() for a in raw.replace(",", ";").split(";") if a.strip()]
    subject = f"{cfg['subject_prefix']} — {today.strftime('%B %-d, %Y')}"
    if args.audience == "test":
        subject = f"[test] {subject}"
    print(f"\nSending to {len(recipients)} recipient(s): {', '.join(recipients)}")
    run([sys.executable, HERE / "send_email.py", "--to", ";".join(recipients),
         "--subject", subject, "--body-file", html, "--importance", "Normal"], "email send")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
