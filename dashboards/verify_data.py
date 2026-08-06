#!/usr/bin/env python3
"""Pre-build gate for the PMN KPI dashboard: prove the data is (a) actually refreshed
today and (b) reconciles with the source Power BI report before any chart is drawn.

Added 2026-08-05 after a run shipped with July AI numbers that disagreed with the
R&D Efficiency report (20 AI epics vs the report's 23) and with the current month
missing from both AI charts. Both failures were silent -- the dashboard looked fine.
This script makes them loud.

Usage:
    python3 verify_data.py --data data.json --report report_values.json [--today YYYY-MM-DD]

Exit codes: 0 = all checks passed, 1 = at least one FAIL. WARNs never fail the run.
Run it BEFORE build_charts.py. If it exits 1, fix the data -- do not build and send.

report_values.json = what was actually read off the Power BI report on THIS run
(hand-transcribed from the browser; that is the point -- it forces the numbers to be
looked at rather than assumed):
{
  "last_synced": "2026-08-05",                    # "Last Synced Time (UTC)" on the report header
  "edct": {"2026/04": 7, "2026/08": 14},          # Epic Dev Cycle Time page, KPI-TREND, AI-Agent=Yes
  "ai_usage_trend": {                             # "% Issues Developed by AI Agents" page
      "2026/07": {"ai": 23, "total": 26},
      "2026/08": {"ai": 1,  "total": 1}
  },
  "issues_with_pr_id": {"2026/Q2": 23, "2026/Q3": 48}   # "AI Fields Adaption" page (quarterly)
}
Any key may be omitted -- omitted sections are reported as SKIPPED, not passed.
"""
import argparse
import datetime as dt
import json
import sys

MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

results = []
SNAPSHOT_STALE_OK = False
SNAPSHOT_MONTH = None       # (year, month) of the report snapshot; months >= this are "open"


def drift_status(year, month):
    """FAIL normally; WARN when reconciling an open month against a stale snapshot."""
    if SNAPSHOT_STALE_OK and SNAPSHOT_MONTH and (year, month) >= SNAPSHOT_MONTH:
        return "WARN"
    return "FAIL"


def record(status, check, detail):
    results.append((status, check, detail))


def norm_month(label, default_year):
    """'2026/08*' -> (2026, 8); 'Jul' -> (default_year, 7); 'August' -> (default_year, 8)."""
    s = str(label).strip().rstrip("*").strip()
    if "/" in s:
        y, m = s.split("/")[:2]
        return int(y), int(m)
    key = s.lower()
    if key in MONTH_NAMES:
        return default_year, MONTH_NAMES[key]
    raise ValueError(f"unparseable month label: {label!r}")


def check_freshness(data, report, today):
    """Every section must carry today's as_of. A carried-forward section is a WARN;
    the caption must then say so, which is what the as_of date is for."""
    omitted = data.get("omitted_sections", {})
    for section in ("delivery", "edct", "quality", "ai_epics_pct", "ai_fields_adoption"):
        if section not in data:
            # A section left out on purpose is fine -- but it has to SAY so, the same way
            # omit_current_month does. Silence stays a failure.
            if section in omitted:
                record("PASS", f"freshness:{section}", f"omitted by decision: {omitted[section]}")
            else:
                record("FAIL", f"freshness:{section}", "section missing from data.json entirely")
            continue
        as_of = data[section].get("as_of")
        if as_of is None:
            record("FAIL", f"freshness:{section}", "no as_of date -- cannot tell if this is today's data")
        elif as_of == today.isoformat():
            record("PASS", f"freshness:{section}", f"as_of {as_of}")
        else:
            record("WARN", f"freshness:{section}",
                   f"as_of {as_of}, not today ({today}) -- carried forward; the caption MUST say so")

    synced = (report or {}).get("last_synced")
    if not synced:
        record("SKIP", "freshness:powerbi_sync", "no last_synced in report_values.json")
    elif synced == today.isoformat():
        record("PASS", "freshness:powerbi_sync", f"report synced {synced}")
    else:
        record("WARN", "freshness:powerbi_sync",
               f"report last synced {synced}, not today -- Power BI itself is stale, not just our pull")


def check_current_month(data, today):
    """The bug that prompted this script: the current month silently absent from a
    monthly series. Excluding it can be a deliberate call (too few data points), but
    it must be a decision, not an accident -- so it fails unless the data says
    'omit_current_month' with a stated reason."""
    for section in ("edct", "ai_epics_pct", "ai_fields_adoption"):
        d = data.get(section)
        if not d or "months" not in d:
            record("SKIP", f"current_month:{section}", "no months series")
            continue
        try:
            parsed = [norm_month(m, today.year) for m in d["months"]]
        except ValueError as e:
            record("FAIL", f"current_month:{section}", str(e))
            continue
        if (today.year, today.month) in parsed:
            record("PASS", f"current_month:{section}", f"{today.year}/{today.month:02d} present")
        else:
            reason = d.get("omit_current_month")
            if reason:
                record("WARN", f"current_month:{section}",
                       f"{today.year}/{today.month:02d} deliberately omitted: {reason}")
            else:
                record("FAIL", f"current_month:{section}",
                       f"{today.year}/{today.month:02d} MISSING and no omit_current_month reason given")


def check_edct(data, report, today):
    rep = (report or {}).get("edct")
    if not rep:
        record("SKIP", "reconcile:edct", "no edct in report_values.json")
        return
    d = data.get("edct")
    if not d:
        record("FAIL", "reconcile:edct", "no edct section in data.json")
        return
    # Until EDCT was computed here, data["edct"] WAS the snapshot, so this check
    # compared the snapshot against itself and could never fail. Now that it is
    # derived from changelogs the comparison is real -- and so is a source of
    # legitimate divergence that is not definition drift: the population is
    # "Implemented by AI Agent = Yes", and an epic can be given that flag long after
    # the month closed. One such epic joining with 0 counted days moves a monthly
    # mean by a day. So a small gap is a WARN even on a closed month; a large one
    # still fails, because that is what a real definition change looks like.
    computed = d.get("source") == "jira"
    tolerance = 2
    ours = {}
    for label, val in zip(d["months"], d["values"]):
        y, m = norm_month(label, today.year)
        ours[f"{y}/{m:02d}"] = val
    for key, want in rep.items():
        y, m = norm_month(key, today.year)
        k = f"{y}/{m:02d}"
        got = ours.get(k)
        if got is None:
            record(drift_status(y, m), f"reconcile:edct {k}",
                   f"report has {want}, dashboard has no such month")
        elif got == want:
            record("PASS", f"reconcile:edct {k}", f"{got} == report {want}")
        elif computed and abs(got - want) <= tolerance:
            record("WARN", f"reconcile:edct {k}",
                   f"computed {got} vs snapshot {want} (diff {got - want:+d}) — within tolerance; "
                   f"the AI-Agent population can change after a month closes. "
                   f"Re-read the Power BI page if this grows.")
        else:
            record(drift_status(y, m), f"reconcile:edct {k}",
                   f"dashboard {got} != report {want}"
                   + (f" (diff {got - want:+d}, beyond the ±{tolerance} tolerance — this is "
                      f"definition drift, not a population change)" if computed else ""))


def check_ai_epics(data, report, today):
    rep = (report or {}).get("ai_usage_trend")
    if not rep:
        record("SKIP", "reconcile:ai_epics_pct", "no ai_usage_trend in report_values.json")
        return
    d = data.get("ai_epics_pct")
    if not d:
        record("FAIL", "reconcile:ai_epics_pct", "no ai_epics_pct section in data.json")
        return
    ours = {}
    for label, tot, ai in zip(d["months"], d["total_epics"], d["ai_epics"]):
        y, m = norm_month(label, today.year)
        ours[f"{y}/{m:02d}"] = (tot, ai)
    for key, want in rep.items():
        y, m = norm_month(key, today.year)
        k = f"{y}/{m:02d}"
        if k not in ours:
            record(drift_status(y, m), f"reconcile:ai_epics {k}",
                   f"report has {want['ai']}/{want['total']}, dashboard has no such month")
            continue
        tot, ai = ours[k]
        if (tot, ai) == (want["total"], want["ai"]):
            record("PASS", f"reconcile:ai_epics {k}", f"{ai}/{tot} == report")
        elif tot == want["total"] and 0 < ai - want["ai"] <= max(2, round(0.1 * want["total"])):
            # Direction matters. The snapshot is a point-in-time copy of Power BI, and the
            # AI flag keeps getting set on already-resolved epics, so Jira drifting UPWARD
            # by a little -- same denominator, a few more flagged -- is people tagging work,
            # not a broken query. Drifting DOWNWARD is the undercount signature (the label
            # bug always read low), so that stays a hard failure below, closed month or not.
            record("WARN", f"reconcile:ai_epics {k}",
                   f"dashboard {ai}/{tot} vs report {want['ai']}/{want['total']} "
                   f"(+{ai - want['ai']}) -- Jira ahead of the snapshot; an epic was flagged "
                   f"AI after the pull. Re-sync the Power BI snapshot when convenient.")
        else:
            record("FAIL" if ai < want["ai"] else drift_status(y, m), f"reconcile:ai_epics {k}",
                   f"dashboard {ai}/{tot} != report {want['ai']}/{want['total']}"
                   + (f" -- dashboard reads LOW, which is the AGENTIC_AI_CODE-label "
                      f"undercount signature; the series must use cf[15229] "
                      f"'Implemented by AI Agent' (the 2026-08-05 bug)"
                      if ai < want["ai"] else " -- unexpected divergence, check both sources"))


def check_ai_fields(data, report, today, tolerance=3):
    """The report's AI Fields Adaption page is QUARTERLY, so reconcile our monthly
    'having AI metrics' series by summing it into quarters. A small gap is normal --
    the report is a snapshot and Jira is live -- so allow +/-tolerance as a WARN."""
    rep = (report or {}).get("issues_with_pr_id")
    if not rep:
        record("SKIP", "reconcile:ai_fields_adoption", "no issues_with_pr_id in report_values.json")
        return
    d = data.get("ai_fields_adoption")
    if not d:
        record("FAIL", "reconcile:ai_fields_adoption", "no ai_fields_adoption section in data.json")
        return
    quarters = {}
    for label, n in zip(d["months"], d["new_metrics"]):
        y, m = norm_month(label, today.year)
        quarters.setdefault(f"{y}/Q{(m - 1) // 3 + 1}", 0)
        quarters[f"{y}/Q{(m - 1) // 3 + 1}"] += n
    for key, want in rep.items():
        got = quarters.get(key)
        if got is None:
            record("FAIL", f"reconcile:pr_id {key}", f"report has {want}, dashboard covers no month in {key}")
        elif got == want:
            record("PASS", f"reconcile:pr_id {key}", f"{got} == report {want}")
        elif abs(got - want) <= tolerance:
            record("WARN", f"reconcile:pr_id {key}",
                   f"dashboard {got} vs report {want} (diff {got - want:+d}, within snapshot tolerance)")
        else:
            qy, qn = int(key.split("/")[0]), int(key.split("/Q")[1])
            status = drift_status(qy, qn * 3)      # last month of that quarter
            record(status, f"reconcile:pr_id {key}",
                   f"dashboard {got} != report {want} (diff {got - want:+d})"
                   + (" -- open quarter vs frozen snapshot" if status == "WARN"
                      else " -- definition drift, not lag"))


def check_internal(data, today):
    d = data.get("delivery")
    if d:
        n = len(d.get("days_into_release_delivered", []))
        if d.get("current_delivered") == n:
            record("PASS", "internal:delivery_count", f"{n} epics, counts agree")
        else:
            record("FAIL", "internal:delivery_count",
                   f"current_delivered={d.get('current_delivered')} but "
                   f"{n} resolution dates supplied")
    q = data.get("quality")
    if q:
        weeks = q.get("weeks", [])
        monday = today - dt.timedelta(days=today.weekday())
        want = monday.strftime("%d%b")
        if weeks and weeks[-1] == want:
            record("PASS", "internal:quality_week", f"last week bucket {weeks[-1]} is the current week")
        else:
            record("FAIL", "internal:quality_week",
                   f"last week bucket is {weeks[-1] if weeks else 'none'}, expected {want} "
                   f"-- the bug trend is not covering this week")
        for name in ("created", "resolved", "open_trend"):
            if len(q.get(name, [])) != len(weeks):
                record("FAIL", f"internal:quality_{name}", "series length != number of weeks")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--report", default=None,
                    help="JSON of values read off the Power BI report on this run")
    ap.add_argument("--today", default=None, help="override today's date (YYYY-MM-DD)")
    ap.add_argument("--snapshot-stale-ok", action="store_true",
                    help="The report values are a committed snapshot from an earlier date "
                         "(unattended/CI runs, where Power BI cannot be re-read). Months at or "
                         "after the snapshot month reconcile as WARN instead of FAIL, since live "
                         "Jira legitimately moves ahead of a frozen snapshot. Closed months stay "
                         "strict -- those must never drift.")
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    data = json.load(open(args.data))
    report = json.load(open(args.report)) if args.report else None

    global SNAPSHOT_STALE_OK, SNAPSHOT_MONTH
    SNAPSHOT_STALE_OK = args.snapshot_stale_ok
    if report and report.get("last_synced"):
        snap = dt.date.fromisoformat(report["last_synced"])
        SNAPSHOT_MONTH = (snap.year, snap.month)

    check_freshness(data, report, today)
    check_current_month(data, today)
    check_edct(data, report, today)
    check_ai_epics(data, report, today)
    check_ai_fields(data, report, today)
    check_internal(data, today)

    width = max(len(c) for _, c, _ in results) + 2
    print(f"\nKPI dashboard data verification — {today}\n" + "=" * 78)
    for status, check, detail in results:
        print(f"  [{status:4}] {check:<{width}} {detail}")
    fails = [r for r in results if r[0] == "FAIL"]
    warns = [r for r in results if r[0] == "WARN"]
    print("=" * 78)
    print(f"  {len(fails)} FAIL, {len(warns)} WARN, "
          f"{len([r for r in results if r[0] == 'PASS'])} PASS, "
          f"{len([r for r in results if r[0] == 'SKIP'])} SKIP")
    if fails:
        print("\n  DO NOT BUILD OR SEND. Fix the data first:")
        for _, check, detail in fails:
            print(f"    - {check}: {detail}")
        return 1
    print("\n  OK to build.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
