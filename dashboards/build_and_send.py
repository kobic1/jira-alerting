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

EVERY SECTION IS LIVE FROM JIRA. Nothing here needs a browser or an interactive
sign-in, so the whole thing runs on GitHub's cloud with no machine of Kobi's
switched on.

EDCT used to be the exception -- it was hand-copied from the "R&D Efficiency"
Power BI app, which sits behind interactive Microsoft SSO. It is now computed
from Jira changelogs by edct_from_jira.py (average calendar days in In Progress
or Validation, excluding flagged days and Maintenance epics), validated to
reproduce that report exactly. `powerbi_snapshot*.json` is kept only as the
reconciliation baseline the verification gate checks the live numbers against,
so a definition drift in a CLOSED month fails the build rather than shipping.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HERE = Path(__file__).resolve().parent

# Field IDs (per-instance, stable across releases -- see the skill's jira_field_ids.md)
CF_IMPLEMENTED_BY_AI = "cf[15229]"   # "Implemented by AI Agent" (Yes/No)
CF_PR_URL = "cf[15262]"              # PR URL -- the "new AI metrics" signal
CF_ISSUE_CATEGORY = "cf[10139]"      # "Issue Category" dropdown -- same field EDCT
                                     # excludes Maintenance on (edct_from_jira.py)
EXCLUDED_CATEGORY = "Maintenance"
CF_CLOSED_DATE = "cf[10099]"          # "Closed" date -- bucketing field for the AI metrics table
CF_CODE_COVERAGE = "customfield_15308"
CF_DEV_DURATION = "customfield_15309"
CF_REVIEW_DURATION = "customfield_15310"

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
        try:
            return int(self._post("/rest/api/3/search/approximate-count",
                                  {"jql": jql}, timeout=60)["count"])
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in (404, 410):
                return len(self.search(jql, ["key"]))
            raise

    def _post(self, path: str, body: dict, timeout: int = 90):
        """POST with a retry around mid-response connection failures.

        urllib3's Retry covers status codes and connection setup, but not a body that
        dies while streaming: requests raises ChunkedEncodingError/ProtocolError after
        the response has already started, which escapes it entirely. That killed the
        2026-08-07 PMN run outright ("Response ended prematurely") — one flaky read out
        of several hundred, and the whole dashboard went unsent.
        """
        last = None
        for attempt in range(4):
            try:
                r = self.s.post(f"{self.base}{path}", json=body, timeout=timeout)
                r.raise_for_status()
                return r.json()
            except (requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ConnectionError,
                    urllib3.exceptions.ProtocolError,
                    json.JSONDecodeError) as e:
                last = e
                if attempt == 3:
                    break
                wait = 2 ** attempt
                print(f"  transient read error ({type(e).__name__}), retry "
                      f"{attempt + 1}/3 in {wait}s", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"Jira POST {path} failed after 4 attempts: {last}")

    def search(self, jql: str, fields: list[str]) -> list[dict]:
        """All issues matching jql, following nextPageToken to the end."""
        out: list[dict] = []
        token = None
        while True:
            body = {"jql": jql, "fields": fields, "maxResults": 100}
            if token:
                body["nextPageToken"] = token
            data = self._post("/rest/api/3/search/jql", body)
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


def team_clauses(cfg: dict) -> tuple[str, str]:
    """JQL fragments that narrow a config to one team, or ('', '') for a whole project.

    PMN records the team in two different fields depending on issue type -- cf[10040]
    'Epic Team\\s' on epics, cf[10098] 'Team Name' on stories/bugs/tasks -- so a
    single-field filter silently drops most of the data. `any_clause` is the union
    (epics and all-issue-type series); `bug_clause` is what bugs actually carry.
    """
    ts = cfg.get("team_scope")
    if not ts:
        return "", ""
    return f' AND {ts["any_clause"]}', f' AND {ts["bug_clause"]}'


def jira_link(j: "Jira", jql: str) -> str:
    """A clickable Jira issue-search URL for the exact JQL a chart value came from."""
    return f"{j.base}/issues?jql={urllib.parse.quote(jql)}"


# ------------------------------------------------------------------- data sections

def build_delivery(j: Jira, cfg: dict, today: dt.date) -> dict:
    proj, rel = cfg["project"], cfg["release"]
    start = d(cfg["release_start_date"])
    team, _ = team_clauses(cfg)
    delivered_jql = (f'project = {proj} AND fixVersion = "{rel}" AND issuetype = Epic '
                      f'AND status = Done{team}')
    issues = j.search(delivered_jql, ["resolutiondate"])
    days = []
    for i in issues:
        rd = d((i.get("fields") or {}).get("resolutiondate"))
        if rd:
            days.append(max(0, (rd - start).days))
    days.sort()
    scope: dict = {}
    if cfg.get("team_scope"):
        # For a single team the in-flight breakdown is the story -- "1 of 22" alone
        # reads as a stall when five epics are mid-development.
        for i in j.search(f'project = {proj} AND fixVersion = "{rel}" AND issuetype = Epic{team}',
                          ["status"]):
            name = ((i.get("fields") or {}).get("status") or {}).get("name", "Unknown")
            scope[name] = scope.get(name, 0) + 1
    return {
        "release_history": cfg["release_history"],
        "release_order": cfg["release_order"],
        "current_delivered": len(days),
        "release_start_date": cfg["release_start_date"],
        "release_end_date": cfg["release_end_date"],
        "sprint_boundaries": cfg["sprint_boundaries"],
        "days_into_release_delivered": days,
        "forecast_override": cfg.get("forecast_override"),
        "scope_breakdown": scope,
        # For the "Epics delivered" stat card link -- the delivered-epics search
        # itself, not a per-chart-point link (the delivery chart has no image map).
        "delivered_link": jira_link(j, delivered_jql),
        "as_of": today.isoformat(),
    }


def build_quality(j: Jira, cfg: dict, today: dt.date, weeks_back: int = 9) -> dict:
    """Weekly created / resolved / running-open for the full bug backlog, excluding
    Accessibility. The `resolution is EMPTY` clause is what makes the opening
    baseline trustworthy -- without it, long-open bugs created before the window
    are missed and the Total Open line starts too low."""
    this_monday = today - dt.timedelta(days=today.weekday())
    weeks = [this_monday - dt.timedelta(days=7 * i) for i in range(weeks_back - 1, -1, -1)]
    ws = weeks[0]
    # Extra per-project exclusions (e.g. CXCO's DWP_Accessibility_Defect batch) keep this
    # chart on the same basis as the [Accessibility] summary exclusion.
    extra = cfg.get("bug_exclusions", "")
    _, team = team_clauses(cfg)
    jql = (f'project = {cfg["project"]} AND issuetype = Bug '
           f'AND summary !~ "\\"[Accessibility]*\\"" {extra}{team} '
           f'AND (created >= "{ws}" OR resolutiondate >= "{ws}" OR resolution is EMPTY)')
    issues = j.search(jql, ["created", "resolutiondate"])
    recs = [(d(i["fields"].get("created")), d(i["fields"].get("resolutiondate")))
            for i in issues]

    baseline = sum(1 for c, r in recs if c and c < ws and (r is None or r >= ws))
    created, resolved, open_trend, open_trend_links = [], [], [], []
    run = baseline
    for w in weeks:
        e = w + dt.timedelta(days=7)
        c = sum(1 for cc, _ in recs if cc and w <= cc < e)
        rr = sum(1 for _, r in recs if r and w <= r < e)
        run += c - rr
        created.append(c)
        resolved.append(rr)
        open_trend.append(run)
        # Open-as-of-week-end, reconstructed as its own JQL rather than a JQL "list all
        # weeks" query -- created before the cutoff and either still open or resolved
        # after it. This is the same baseline+running-total logic above, just phrased
        # as a standalone search so the "Total Open" trend-line label can link to it.
        open_trend_links.append(jira_link(j,
            f'project = {cfg["project"]} AND issuetype = Bug '
            f'AND summary !~ "\\"[Accessibility]*\\"" {extra}{team} '
            f'AND created < "{e}" AND (resolutiondate is EMPTY OR resolutiondate >= "{e}")'))
    return {
        "weeks": [w.strftime("%d%b") for w in weeks],
        "created": created, "resolved": resolved, "open_trend": open_trend,
        "open_trend_links": open_trend_links,
        "as_of": today.isoformat(),
    }


def build_ai_epics(j: Jira, cfg: dict, today: dt.date) -> dict:
    """Reproduces the Power BI '% AI Usage Trend' chart. AI epics are the
    cf[15229] Implemented-by-AI-Agent FIELD, not the AGENTIC_AI_CODE label --
    the label undercounts (July 2026: 20 by label vs 23 by field, and the
    report says 23). Maintenance-category epics are excluded (Kobi, 2026-09-03),
    the same exclusion EDCT already applies -- a maintenance epic being AI-assisted
    or not says little about feature-development AI adoption, and without this the
    denominator was pulling in epics none of the other sections count."""
    months, totals, ais, pct_links = [], [], [], []
    full = cfg.get("ai_epics_month_style", "short") == "full"
    team, _ = team_clauses(cfg)
    for m in range(cfg.get("ai_epics_first_month", 1), today.month + 1):
        lo, hi = month_bounds(today.year, m)
        window = f'AND resolutiondate >= "{lo}" AND resolutiondate < "{hi}"'
        base = (f'project = {cfg["project"]} AND issuetype = Epic AND status = Done{team} '
                f'AND ({CF_ISSUE_CATEGORY} is EMPTY OR {CF_ISSUE_CATEGORY} != "{EXCLUDED_CATEGORY}") '
                f'{window}')
        label = calendar.month_name[m] if full else MONTHS_SHORT[m - 1]
        months.append(label + ("*" if m == today.month else ""))
        totals.append(j.count(base))
        ai_jql = f'{base} AND {CF_IMPLEMENTED_BY_AI} = "Yes"'
        ais.append(j.count(ai_jql))
        # The %-line label links to its numerator (the AI-developed epics that made
        # the rate), the more useful drill-through than the plain total.
        pct_links.append(jira_link(j, ai_jql))
    return {"_definition": "Total = epics Done resolved in month; AI = same with "
                           "cf[15229] Implemented by AI Agent = Yes (the report's definition).",
            "months": months, "total_epics": totals, "ai_epics": ais, "pct_links": pct_links,
            "as_of": today.isoformat()}


def build_edct(cfg: dict, today: dt.date) -> dict:
    """EDCT straight from Jira changelogs -- see edct_from_jira.py for the metric.

    This is what lets EDCT refresh headlessly (and lets a team-scoped dashboard have
    an EDCT section at all): the Power BI page needs an interactive sign-in, the
    changelog does not. Months where the population is empty are dropped rather than
    plotted as zero -- no epics resolved is not a cycle time of nothing.
    """
    import edct_from_jira as E

    j = E.Jira()
    ai_only = cfg.get("edct_ai_only", True)
    team = (cfg.get("team_scope") or {}).get("any_clause")
    months, values, links = [], [], []
    empty_current = None
    for m in range(cfg.get("edct_first_month", 4), today.month + 1):
        lo, hi = month_bounds(today.year, m)
        jql = (f'project = {cfg["project"]} AND issuetype = Epic AND status = Done '
               f'AND resolutiondate >= "{lo}" AND resolutiondate < "{hi}"')
        if ai_only:
            jql += f' AND {CF_IMPLEMENTED_BY_AI} = "Yes"'
        if team:
            jql += f' AND {team}'
        vals = []
        for i in j.search(jql, ["resolutiondate", E.CF_ISSUE_CATEGORY]):
            f = i["fields"]
            cat = f.get(E.CF_ISSUE_CATEGORY)
            cat = cat.get("value") if isinstance(cat, dict) else cat
            if cat == E.EXCLUDED_CATEGORY:
                continue
            vals.append(E.edct_days(E.epic_events(j.changelog(i["key"])),
                                    end=E.ts(f["resolutiondate"]).date(),
                                    count_done_day=cfg.get("edct_count_done_day", False)))
        if not vals:
            if m == today.month:
                empty_current = "no epics resolved yet this month, so there is no average to plot"
            continue
        months.append(f"{today.year}/{m:02d}" + ("*" if m == today.month else ""))
        values.append(round(sum(vals) / len(vals)))
        # Same population as `vals`, minus the Maintenance-category exclusion (that
        # filter runs client-side against the changelog, but is expressible in JQL
        # too) -- for the "EDCT days" stat card link.
        links.append(jira_link(j, f'{jql} AND ({E.CF_ISSUE_CATEGORY} is EMPTY OR '
                                   f'{E.CF_ISSUE_CATEGORY} != "{E.EXCLUDED_CATEGORY}")'))
    out = {"months": months, "values": values, "links": links,
           "source": "jira",
           "target_all": cfg.get("edct_target", 10), "target_ai": None,
           "_definition": "Average calendar days in In Progress or Validation, excluding "
                          "flagged days and Maintenance-category epics, computed from Jira "
                          "changelogs. Matches the R&D Efficiency report's KPI-TREND row.",
           "as_of": today.isoformat()}
    if empty_current:
        out["omit_current_month"] = empty_current
    return out


def build_ai_fields(j: Jira, cfg: dict, today: dt.date, first_month: int = 4) -> dict:
    """Marked-as-AI vs having-the-PR-URL-metric, monthly. Scope matches the Power BI
    'AI Fields Adaption' page filters: ALL issue types, ALL statuses, by resolved
    date -- so the metrics series sums to the report's quarterly '# Issues with PR
    ID'. Narrowing this to Done Stories/Bugs understates it badly."""
    months, marked, metrics, pct_links = [], [], [], []
    team, _ = team_clauses(cfg)
    for m in range(first_month, today.month + 1):
        lo, hi = month_bounds(today.year, m)
        window = f'resolutiondate >= "{lo}" AND resolutiondate < "{hi}"'
        months.append(calendar.month_name[m] + ("*" if m == today.month else ""))
        proj = cfg["project"]
        marked.append(j.count(f'project = {proj}{team} AND {CF_IMPLEMENTED_BY_AI} = "Yes" AND {window}'))
        metrics_jql = f'project = {proj}{team} AND {CF_PR_URL} is not EMPTY AND {window}'
        metrics.append(j.count(metrics_jql))
        # The %-line label links to its numerator (issues that actually carry the new
        # metric), the more useful drill-through than the marked-as-AI denominator.
        pct_links.append(jira_link(j, metrics_jql))
    return {"_definition": "Marked as AI = cf[15229] = Yes; having metrics = cf[15262] PR-URL "
                           "populated. All issue types, all statuses, by resolved date.",
            "months": months, "marked_ai": marked, "new_metrics": metrics, "pct_links": pct_links,
            "as_of": today.isoformat()}


def build_ai_metrics_table(j: Jira, cfg: dict, today: dt.date) -> dict:
    """The jira-implementer-stat skill's cohort table (Issues Marked as AI, Having AI
    Metrics Stats, %, median Dev/Review duration, average Code Coverage) for EPICS and
    BUG+STORY, all teams combined -- Kobi asked for "just the table as is, without any
    filtering ability" (2026-09-03), not the interactive multi-team dashboard the skill
    also supports. Bucketed by the Closed date field (cf[10099]), confirmed populated
    for this project; last 4 calendar months including the current partial one."""
    proj = cfg["project"]
    FIELDS = ["customfield_15262", CF_CODE_COVERAGE, CF_DEV_DURATION,
              CF_REVIEW_DURATION, "customfield_10099"]
    GROUPS = [("epics", "issuetype = Epic"), ("bug_story", "issuetype in (Story, Bug)")]

    months: list[tuple[int, int]] = []
    y, m = today.year, today.month
    for _ in range(4):
        months.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    months.reverse()
    lo, _ = month_bounds(*months[0])
    _, hi = month_bounds(*months[-1])

    def median(vals: list[float]) -> float | None:
        vals = sorted(vals)
        n = len(vals)
        if not n:
            return None
        mid = n // 2
        return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2

    result: dict = {
        "months": [MONTHS_SHORT[mo - 1] + ("*" if (yr, mo) == (today.year, today.month) else "")
                   for yr, mo in months],
        "as_of": today.isoformat(),
    }
    for key, type_clause in GROUPS:
        jql = (f'project = {proj} AND {type_clause} AND status = Done '
               f'AND {CF_IMPLEMENTED_BY_AI} = "Yes" '
               f'AND {CF_CLOSED_DATE} >= "{lo}" AND {CF_CLOSED_DATE} < "{hi}"')
        buckets: dict[tuple[int, int], list[dict]] = {ym: [] for ym in months}
        bounds = {ym: (d(month_bounds(*ym)[0]), d(month_bounds(*ym)[1])) for ym in months}
        for i in j.search(jql, FIELDS):
            f = i["fields"]
            closed = d(f.get("customfield_10099"))
            if not closed:
                continue
            for ym, (b_lo, b_hi) in bounds.items():
                if b_lo <= closed < b_hi:
                    buckets[ym].append(f)
                    break
        marked, having, pct_vals, med_dev, med_rev, avg_cov = [], [], [], [], [], []
        for ym in months:
            items = buckets[ym]
            marked.append(len(items))
            hv = sum(1 for f in items if f.get("customfield_15262"))
            having.append(hv)
            pct_vals.append(100.0 * hv / len(items) if items else 0.0)
            med_dev.append(median([f[CF_DEV_DURATION] for f in items
                                    if f.get(CF_DEV_DURATION) is not None]))
            med_rev.append(median([f[CF_REVIEW_DURATION] for f in items
                                    if f.get(CF_REVIEW_DURATION) is not None]))
            # A recorded 0 is treated as "not measured", not a genuine 0% coverage --
            # `if f.get(...)` excludes both None and 0 since both are falsy.
            cov = [f[CF_CODE_COVERAGE] for f in items if f.get(CF_CODE_COVERAGE)]
            avg_cov.append(sum(cov) / len(cov) if cov else None)
        result[key] = {"marked_ai": marked, "having_metrics": having, "pct": pct_vals,
                        "median_dev_min": med_dev, "median_review_min": med_rev,
                        "avg_coverage_pct": avg_cov}
    return result


# ------------------------------------------------------------------------ captions

def pct(a: int, b: int) -> float:
    return 100.0 * a / b if b else 0.0


def short_month(label: str) -> str:
    """Normalize any of this file's three month-label formats ("2026/08", "August",
    "Aug", each possibly with a trailing '*' for the partial current month) to the
    short "Aug"/"Aug*" form. Without this, three stat cards for the same month can
    each print it differently ("2026/08" / "August" / "Aug") side by side, which
    reads as three different months at a glance, not one."""
    star = "*" if label.endswith("*") else ""
    body = label.rstrip("*")
    if "/" in body:
        return MONTHS_SHORT[int(body.split("/")[1]) - 1] + star
    return body[:3] + star


def build_meta(data: dict, cfg: dict, snapshot: dict, today: dt.date) -> dict:
    proj = cfg["project"]
    dl, ql = data["delivery"], data["quality"]
    ae = data["ai_epics_pct"]
    af = data.get("ai_fields_adoption")      # absent where the project hasn't instrumented it
    ed = data.get("edct")

    day_of_release = (today - d(cfg["release_start_date"])).days
    forecast = dl.get("forecast_override") or 0
    last_close = d(cfg["release_start_date"]) + dt.timedelta(days=max(dl["days_into_release_delivered"] or [0]))

    def last_closed(series: dict) -> int:
        """Index of the newest month that is NOT partial. A partial month is marked
        with a trailing '*', so trust that rather than assuming it is always the last
        entry -- a month with an empty population is dropped from the EDCT series, and
        a blind -2 then reports the month before last as 'closed'."""
        months = series["months"]
        for i in range(len(months) - 1, -1, -1):
            if not str(months[i]).endswith("*"):
                return i
        return len(months) - 1

    def headline_index(series: dict) -> int:
        """Index of the month the headline stat should show. Always the current
        month once it exists (marked '*' for partial), however few days it has
        behind it -- Kobi confirmed (2026-09-03) he'd rather see a fresh 1-3 day
        sample than a stat card stuck on last month. Falls back to last_closed()
        only when the series has no partial current month at all."""
        months = series["months"]
        if months and str(months[-1]).endswith("*"):
            return len(months) - 1
        return last_closed(series)

    # AI headline figures use the current month once it has enough days behind it to
    # be a rate, not a 1-3 day sample -- see headline_index().
    closed = headline_index(ae)
    ae_pct_closed = pct(ae["ai_epics"][closed], ae["total_epics"][closed])
    ae_cur = pct(ae["ai_epics"][-1], ae["total_epics"][-1])
    if af:
        af_closed = headline_index(af)
        af_pct_closed = pct(af["new_metrics"][af_closed], af["marked_ai"][af_closed])
        af_cur = pct(af["new_metrics"][-1], af["marked_ai"][-1])

    team = (cfg.get("team_scope") or {}).get("name")
    peak = max(ql["open_trend"])
    peak_week = ql["weeks"][ql["open_trend"].index(peak)]
    edct_stale = ed and ed.get("as_of") != today.isoformat()
    ed_closed = headline_index(ed) if ed else -1

    # Without EDCT the third card would be a dead em-dash; for a team cut the count of
    # epics actually moving is the more useful number in that slot.
    sb = dl.get("scope_breakdown") or {}
    ed_links = ed.get("links") or [] if ed else []
    if ed:
        # Say which month this is -- silently showing last month's number with no
        # qualifier read as a bug (Kobi flagged it, 2026-09-03) once "Epics by AI"
        # started doing this and this card didn't: two cards, two different time
        # bases, neither one saying so.
        third = {"num": str(ed["values"][ed_closed]),
                 "lbl": f"EDCT days ({short_month(ed['months'][ed_closed])})"}
        if ed_closed < len(ed_links):
            third["href"] = ed_links[ed_closed]
    elif sb:
        third = {"num": str(sb.get("In Progress", 0) + sb.get("Validation", 0)),
                 "lbl": "In dev / validation"}
    else:
        third = {"num": "&mdash;", "lbl": "EDCT days"}

    # Charts stay plain images (HTML image maps confirmed not to render as clickable
    # in Kobi's mail client), so the stat cards -- real HTML text, not raster -- are
    # where "click to see the Jira issues" actually works. Each href reuses the exact
    # JQL that produced the number above it.
    open_bugs_links = ql.get("open_trend_links") or []
    stats = [
        {"num": str(dl["current_delivered"]), "lbl": "Epics delivered",
         **({"href": dl["delivered_link"]} if dl.get("delivered_link") else {})},
        {"num": str(forecast), "lbl": "Target epics" if team else "Forecast epics"},
        third,
        {"num": str(ql["open_trend"][-1]), "lbl": "Open bugs",
         **({"href": open_bugs_links[-1]} if open_bugs_links else {})},
        {"num": f"{af_pct_closed:.1f}%" if af else "&mdash;",
         "lbl": f"AI field adoption ({short_month(af['months'][af_closed])})" if af else "AI field adoption",
         **({"href": af["pct_links"][af_closed]} if af and af_closed < len(af.get("pct_links") or []) else {})},
        {"num": f"{ae_pct_closed:.1f}%", "lbl": f"Epics by AI ({short_month(ae['months'][closed])})",
         **({"href": ae["pct_links"][closed]} if closed < len(ae.get("pct_links") or []) else {})},
    ]

    # Where a team is small, the in-flight breakdown is the story: "1 of 22" on its own
    # reads as a stall when five epics are mid-development.
    scope_bit = ""
    if dl.get("scope_breakdown"):
        order = ["In Progress", "Validation", "Ready for Dev", "In Definition", "New"]
        phrase = {"In Progress": "in development", "Validation": "in validation",
                  "Ready for Dev": "ready for dev", "In Definition": "in definition",
                  "New": "not started"}
        sb = dl["scope_breakdown"]
        parts = [f"{sb[s]} {phrase[s]}" for s in order if sb.get(s)]
        if parts:
            scope_bit = f" Behind it: {', '.join(parts)}."

    sections = [{
        "file": "chart_delivery.png", "rail": "#1F3A5F", "tint": "rgba(31,58,95,0.06)",
        "seclabel": "Delivery",
        "title": "Epic Delivery &mdash; Actual vs. " + ("Target" if team else "Expected"),
        "caption": (f"{dl['current_delivered']} delivered on day {day_of_release} of the release, "
                    f"against {forecast} ({cfg.get('forecast_basis', 'committed scope')})."
                    + (f" Last epic closed {last_close.strftime('%b %-d')}."
                       if dl["days_into_release_delivered"] else "")
                    + scope_bit + f" Live from {proj} Jira."),
    }]
    if ed:
        sections.append({
            "file": "chart_edct.png", "rail": "#C77700", "tint": "rgba(199,119,0,0.06)",
            "seclabel": "Efficiency", "title": "Epic Dev Cycle Time (EDCT)",
            "caption": (f"Monthly avg cycle time, Implemented-by-AI-Agent view, against the "
                        f"&le;{ed.get('target_all', 10)}"
                        + (f" (all epics) / &le;{ed['target_ai']} (AI-assisted) targets. "
                           if ed.get("target_ai") is not None else " day target. ")
                        # "closed at" is wrong once ed_closed is the still-running current
                        # month -- say "stands at ... month-to-date" instead, same distinction
                        # as the AI captions above.
                        + (f"{ed['months'][ed_closed]} stands at {ed['values'][ed_closed]} days "
                           f"month-to-date. "
                           if ed_closed == len(ed["months"]) - 1 and str(ed["months"][-1]).endswith("*")
                           else f"{ed['months'][ed_closed]} closed at {ed['values'][ed_closed]} days. ")
                        + (f"Computed live from Jira changelogs &mdash; average calendar days in "
                           f"In Progress or Validation, excluding flagged days and "
                           f"Maintenance-category epics. Reconciles with the R&amp;D Efficiency "
                           f"report's KPI-TREND row."
                           if cfg.get("edct_source") == "jira" else
                           (f"<b>Carried forward &mdash; as of {ed['as_of']}.</b> EDCT comes from the "
                            f"R&amp;D Efficiency Power BI report, which needs an interactive "
                            f"sign-in and cannot be refreshed by this automated run; every other "
                            f"section below is live. "
                            if edct_stale else "Pulled live from R&amp;D Efficiency Power BI. ")
                           + "Source: R&amp;D Efficiency Power BI, Epic Dev Cycle Time.")),
        })
    sections.append({
        "file": "chart_quality.png", "rail": "#2E9E4F", "tint": "rgba(46,158,79,0.06)",
        "seclabel": "Quality",
        "title": f"Open Bugs Trend &mdash; {team}" if team else "Open Bugs Trend &mdash; Full Backlog",
        "caption": (f"{ql['open_trend'][-1]} open "
                    + (f"{team} bugs (excl. Accessibility" if team
                       else "in the full-project backlog (excl. Accessibility")
                    + (f"; {cfg['bug_exclusions_note']}" if cfg.get("bug_exclusions_note") else "")
                    + f") &mdash; against a {peak_week} peak of {peak}. The week of "
                    f"{ql['weeks'][-1]} is still running ({ql['created'][-1]} opened, "
                    f"{ql['resolved'][-1]} closed). Live from {proj} Jira."),
    })
    sections.append({
        "file": "chart_ai_epics_pct.png", "rail": "#7B4FC7", "tint": "rgba(123,79,199,0.06)",
        "seclabel": "AI Adoption", "title": "% Epics Developed by AI Agents",
        "caption": (f"{ae_pct_closed:.0f}% of epics AI-developed in {ae['months'][closed]} "
                    f"({ae['ai_epics'][closed]} of {ae['total_epics'][closed]}); "
                    # A month with no resolved epics has no rate -- "0 of 0 (0%)" reads as a
                    # collapse in adoption when it only means nothing has closed yet. And once
                    # headline_index() has already picked the current month (closed == -1), a
                    # second "month-to-date" sentence about that same month would just repeat
                    # the first one -- only add it when it's telling you about a DIFFERENT month.
                    + ("" if closed == len(ae["months"]) - 1 else
                       f"no epics resolved yet in {ae['months'][-1]}. "
                       if not ae["total_epics"][-1] else
                       f"{ae['months'][-1]} stands at {ae['ai_epics'][-1]} of "
                       f"{ae['total_epics'][-1]} ({ae_cur:.0f}%) month-to-date. ")
                    + f"Counted the way the report counts it &mdash; "
                    f"the Implemented-by-AI-Agent field, not the AGENTIC_AI_CODE label. "
                    f"Live from {proj} Jira."),
    })
    if af:
        sections.append({
            "file": "chart_ai_fields_adoption.png", "rail": "#7B4FC7", "tint": "rgba(123,79,199,0.06)",
            "seclabel": "AI Adoption", "title": "AI Fields Adoption (All Issue Types)",
            "caption": (f"{af_pct_closed:.1f}% of AI-marked issues carry the PR-URL metric field in "
                        f"{af['months'][af_closed]} ({af['new_metrics'][af_closed]} of "
                        f"{af['marked_ai'][af_closed]}); "
                        # Denominator can legitimately be 0 early in a month, which would
                        # otherwise print an impossible "1 of 0". And skip the second sentence
                        # entirely once af_closed already IS the current month -- see the
                        # matching comment on the % Epics by AI caption above.
                        + ("" if af_closed == len(af["months"]) - 1 else
                           f"no AI-marked issues resolved yet in {af['months'][-1]}. "
                           if not af["marked_ai"][-1] else
                           f"{af_cur:.1f}% in {af['months'][-1]} so far "
                           f"({af['new_metrics'][-1]} of {af['marked_ai'][-1]}). ")
                        + f"Scope matches the "
                        f"report's AI Fields Adaption page &mdash; all issue types and statuses by "
                        f"resolved date. Live from {proj} Jira."),
        })
    live = "Delivery, Quality and both AI sections" if af else "Delivery, Quality and % Epics by AI"
    scope_label = (f"{cfg.get('project_label', proj)} Jira, {team} team"
                   if team else f"{cfg.get('project_label', proj)} Jira")
    # "reconciled" overstates what actually happens: the AI/EDCT figures are cross-checked
    # against a hand-pulled Power BI snapshot, and a mismatch is logged as a WARN rather than
    # blocking the send -- so a stale snapshot (this one is from {last_synced}, not today)
    # routinely produces a flagged divergence, not a clean reconciliation. Say that honestly.
    if ed and cfg.get("edct_source") == "jira":
        gate = (f"verified before delivery: per-section freshness, the current month present in "
                f"every series, and every AI figure cross-checked against the R&amp;D Efficiency "
                f"Power BI report (snapshot {snapshot.get('last_synced', 'n/a')} -- divergences "
                f"are logged as warnings, not hidden). {live}, plus EDCT, are all computed live "
                f"from Jira; the Power BI report is used only as a cross-check, not as EDCT's "
                f"source.")
    elif ed:
        gate = (f"verified before delivery: per-section freshness, the current month present in "
                f"every series, and every AI/EDCT figure cross-checked against the R&amp;D "
                f"Efficiency Power BI report (snapshot {snapshot.get('last_synced', 'n/a')} -- "
                f"divergences are logged as warnings, not hidden). {live} are live Jira; EDCT is "
                f"from Power BI, dated in its caption above.")
    else:
        # Team-scoped EDCT lives behind the Power BI TEAM filter, which needs an
        # interactive sign-in -- say so rather than shipping a stale or invented figure.
        gate = (f"verified before delivery: per-section freshness and the current month present in "
                f"every series. {live} are live Jira. <b>Epic Dev Cycle Time is not included:</b> "
                f"team-scoped EDCT is only available from the R&amp;D Efficiency Power BI report, "
                f"which needs an interactive sign-in and cannot be read by this automated run.")
    out = {
        "release": cfg["release"], "project": proj,
        "as_of_label": today.strftime("%b %-d %Y"),
        "stats": stats, "sections": sections,
        "footer": (f"Built from {scope_label} on {today.strftime('%b %-d %Y')}, and {gate} "
                   f"Months marked * are partial."
                   + (f" {cfg['footer_note']}" if cfg.get("footer_note") else "")),
    }

    amt = data.get("ai_metrics_table")
    if amt:
        def fmt1(x):
            """Thousands separator, at most one decimal, trailing '.0' dropped."""
            if x is None:
                return "&mdash;"
            s = f"{x:,.1f}"
            return s[:-2] if s.endswith(".0") else s

        def group_rows(g: dict) -> dict:
            return {
                "Issues Marked as AI": [str(v) for v in g["marked_ai"]],
                "Having AI Metrics Stats (PR URL)": [str(v) for v in g["having_metrics"]],
                "%": [f"{v:.1f}%" for v in g["pct"]],
                "Median Development Time (min)": [fmt1(v) for v in g["median_dev_min"]],
                "Median Review Time (min)": [fmt1(v) for v in g["median_review_min"]],
                "Average Code Coverage (%)": [fmt1(v) for v in g["avg_coverage_pct"]],
            }

        out["ai_metrics_table"] = {
            "months": amt["months"],
            "epics_rows": group_rows(amt["epics"]),
            "bug_story_rows": group_rows(amt["bug_story"]),
            "as_of": amt["as_of"],
        }
    return out


# ---------------------------------------------------------------------------- main

def mask(addr):
    """first.last@nice.com -> f****.l****@nice.com. Enough to eyeball a distribution
    list in a public CI log without publishing everyone's address."""
    try:
        local, domain = addr.split("@", 1)
    except ValueError:
        return "***"
    parts = [(p[0] + "*" * max(1, len(p) - 1)) if p else "" for p in local.split(".")]
    return ".".join(parts) + "@" + domain


def resolve_audience(cfg, audience, today):
    """Turn --audience auto into a concrete audience for today.

    One daily cron drives every dashboard, and each config says which weekdays are
    its manager-send days. On any other day a config may still want a personal copy
    (Kobi's daily PMN dashboard); the rest simply do not run. This is what removes
    the old double-send, where a daily local task and a Mon/Thu CI job both mailed
    the PMN dashboard on Mondays and Thursdays.
    """
    if audience != "auto":
        return audience
    manager_days = cfg.get("manager_send_days", [0, 3])       # Mon, Thu
    if today.weekday() in manager_days:
        return "managers"
    return "personal" if cfg.get("daily_personal_copy") else "skip"


def resolve_recipients(cfg, audience):
    """Addresses come from secrets, never from the (public) repo. None for 'none'."""
    if audience in ("none", "skip"):
        return None
    if audience == "managers":
        var = cfg.get("recipients_env", f"{cfg['project']}_DASHBOARD_RECIPIENTS")
        raw = os.environ.get(var, "")
        if not raw.strip():
            sys.exit(f"FATAL: {var} is not set — refusing to guess the distribution list. "
                     f"Nothing was sent.")
    else:
        # 'test' and 'personal' both go to Kobi; only 'test' brands the subject.
        raw = os.environ.get("PMN_DASHBOARD_TEST_RECIPIENT", "kobi.cohen@nice.com")
    return [a.strip() for a in raw.replace(",", ";").split(";") if a.strip()]



def run(cmd: list[str], label: str) -> None:
    print(f"\n$ {' '.join(str(c) for c in cmd)}", flush=True)
    r = subprocess.run([str(c) for c in cmd])
    if r.returncode != 0:
        sys.exit(f"FATAL: {label} failed (exit {r.returncode}) -- nothing was sent.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audience",
                    choices=["managers", "personal", "test", "none", "auto"], default="test",
                    help="'managers' = the distribution list named by the config's "
                         "recipients_env secret; 'personal' = Kobi's own copy; 'test' = Kobi "
                         "with a [test] subject; 'none' = build and verify, send nothing; "
                         "'auto' = decide from the weekday and the config (what cron uses).")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--today", default=None, help="override today's date (YYYY-MM-DD)")
    ap.add_argument("--config", default="config_pmn.json",
                    help="project config in this directory: config_pmn.json, "
                         "config_cxco.json")
    args = ap.parse_args()

    cfg = json.load(open(HERE / args.config))
    snapshot = json.load(open(HERE / cfg["powerbi_snapshot"]))
    today = d(args.today) if args.today else dt.date.today()
    out = Path(args.outdir or (HERE / "build"))
    (out / "charts").mkdir(parents=True, exist_ok=True)

    audience = resolve_audience(cfg, args.audience, today)
    if audience == "skip":
        print(f"{cfg['project']}: not a send day for this dashboard "
              f"({today:%A}) and no daily personal copy configured — nothing to do.")
        return 0
    recipients = resolve_recipients(cfg, audience)
    if recipients is not None:
        shown = ", ".join(mask(a) for a in recipients)
        print(f"Recipients ({audience}): {len(recipients)} — {shown}")

    start = cfg.get("first_send_date")
    if start and today < d(start) and audience == "managers":
        print(f"Not sending to managers before {start} (today is {today}). "
              f"Scheduled start date not reached -- exiting cleanly.")
        return 0

    print(f"{cfg['project']} dashboard build — {today}  (audience: {audience})")
    j = Jira()

    data = {
        "release": cfg["release"],
        "delivery": build_delivery(j, cfg, today),
        "quality": build_quality(j, cfg, today),
        "ai_epics_pct": build_ai_epics(j, cfg, today),
    }
    # AI Fields Adoption is omitted where the project hasn't instrumented the code-metric
    # fields at all (CXCO) -- an empty chart says less than no chart. Record the omission
    # so the verifier sees a stated decision rather than a missing section.
    if cfg.get("sections", {}).get("ai_fields_adoption", True):
        data["ai_fields_adoption"] = build_ai_fields(j, cfg, today)
    else:
        data.setdefault("omitted_sections", {})["ai_fields_adoption"] = (
            cfg.get("footer_note")
            or f"{cfg['project']} has not instrumented the AI code-metric fields")

    if cfg.get("sections", {}).get("ai_metrics_table"):
        data["ai_metrics_table"] = build_ai_metrics_table(j, cfg, today)

    # EDCT: snapshot only. Flag the current month as a deliberate omission when the
    # snapshot predates it, so the verifier warns instead of failing the build.
    # The committed snapshot is whole-project, so a team-scoped config cannot use it --
    # those configs set sections.edct false and the section is omitted by decision.
    if cfg.get("edct_source") == "jira":
        data["edct"] = build_edct(cfg, today)
    elif cfg.get("sections", {}).get("edct", True):
        edct = dict(snapshot["edct_series"])
        if not any(str(m).rstrip("*").endswith(f"/{today.month:02d}") for m in edct["months"]):
            edct["omit_current_month"] = (f"EDCT is a Power BI figure requiring interactive sign-in; "
                                          f"snapshot is from {edct.get('as_of')}")
        data["edct"] = edct
    else:
        data.setdefault("omitted_sections", {})["edct"] = (
            "team-scoped Epic Dev Cycle Time is only available behind the R&D Efficiency "
            "Power BI TEAM filter, which needs an interactive sign-in")

    report_values = {k: v for k, v in snapshot.items() if k != "edct_series"}
    if cfg.get("team_scope"):
        # The snapshot's EDCT / AI figures are whole-project. Reconciling a single team's
        # numbers against them would be comparing different populations, so those checks
        # are dropped to SKIP rather than made to pass by loosening a tolerance.
        report_values = {k: v for k, v in report_values.items()
                         if k not in ("edct", "ai_usage_trend", "issues_with_pr_id")}

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
    label = f"{cfg['project']} {cfg['team_scope']['name']}" if cfg.get("team_scope") else cfg["project"]
    html = out / f"{label} {cfg['release']} Release Dashboard.html"
    run([sys.executable, HERE / "assemble_dashboard_html.py", "--charts-dir", out / "charts",
         "--meta", meta_path, "--out", html], "HTML assembly")

    if audience == "none":
        print(f"\nBuilt {html} — no send requested.")
        return 0

    subject = f"{cfg['subject_prefix']} — {today.strftime('%B %-d, %Y')}"
    if audience == "test":
        subject = f"[test] {subject}"
    print(f"\nSending to {len(recipients)} recipient(s): {', '.join(recipients)}")
    run([sys.executable, HERE / "send_email.py", "--to", ";".join(recipients),
         "--subject", subject, "--body-file", html, "--importance", "Normal"], "email send")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
