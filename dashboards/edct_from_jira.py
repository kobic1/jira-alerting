#!/usr/bin/env python3
"""Epic Dev Cycle Time (EDCT) computed from Jira changelogs.

Replaces the hand-copied Power BI snapshot: the same metric the R&D Efficiency
report shows, derived from data the API can reach, so it refreshes headlessly.

Definition (from the metric owner):
  Population   Done epics, excluding Issue Category = Maintenance.
  Metric       Average number of CALENDAR days an epic spent in "In Progress" or
               "Validation", excluding days on which it was flagged.
  Not          the elapsed span between first In Progress and Done -- time parked
               in Ready for Dev or In Definition does not count.

Day accounting: each calendar day is judged by the state at the END of that day, so

  - the day an epic ENTERS In Progress counts (it ends the day in In Progress);
  - the day it LEAVES to Ready for Dev does not (it ends the day parked);
  - a day flagged at end of day does not count, and the day the flag is cleared does.

THE DONE DAY -- a known divergence between the written definition and the report.
The worked example counts the day the epic moves to Done (its row 13 is "Done /
Counted Yes", which is what makes that example total 10 rather than 9). The
R&D Efficiency report does not. Measured against PMN 2026 with the AI-Agent view,
skipping the Done day reproduces the report exactly and counting it is uniformly
one day higher -- it is a per-epic constant, so it shows up as +1 on every month:

    month     report   counting Done day   skipping Done day
    2026/04        7                   8                   7
    2026/05       24                  25                  24
    2026/06       26                  27                  26
    2026/07       23                  24                  23
    2026/08        7                   8                   7

`--done-day skip` (the default) therefore matches what management already sees in
Power BI; `--done-day count` matches the written definition and reads one day
higher. The default is deliberate -- a dashboard that silently disagrees with the
report it is meant to mirror is worse than one that is a day conservative -- but
the metric owner should settle which rule is authoritative.

Usage:
    python3 edct_from_jira.py --months 2026-04:2026-08 [--ai-only] [--team-any-clause JQL]
    python3 edct_from_jira.py --self-test        # replays the owner's worked example
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

COUNTED_STATUSES = {"In Progress", "Validation"}
EXCLUDED_CATEGORY = "Maintenance"
CF_ISSUE_CATEGORY = "customfield_10139"
CF_IMPLEMENTED_BY_AI = "cf[15229]"
FLAG_FIELD = "Flagged"


# --------------------------------------------------------------------- core metric

def edct_days(events: list[tuple[dt.datetime, str, str | None]],
              end: dt.date, start_hint: dt.date | None = None,
              count_done_day: bool = False) -> int:
    """Counted days for one epic.

    `events` is (timestamp, kind, value) with kind in {"status", "flag"}; the flag
    value is truthy when raised. `end` is the resolution date. Pure function -- the
    self-test drives it with the owner's example and no Jira at all.
    """
    events = sorted(events, key=lambda e: e[0])
    if not events:
        return 0
    first = start_hint or min(e[0].date() for e in events)
    if first > end:
        return 0

    status: str | None = None
    flagged = False
    counted = 0
    idx = 0
    day = first
    while day <= end:
        # State at the end of this day: apply every event stamped on or before 23:59.
        boundary = dt.datetime.combine(day, dt.time.max, tzinfo=events[0][0].tzinfo)
        entered_done_today = False
        while idx < len(events) and events[idx][0] <= boundary:
            _, kind, value = events[idx]
            if kind == "status":
                if value == "Done" and status in COUNTED_STATUSES:
                    entered_done_today = True
                status = value
            else:
                flagged = bool(value)
            idx += 1
        if not flagged and (status in COUNTED_STATUSES
                            or (count_done_day and entered_done_today)):
            counted += 1
        day += dt.timedelta(days=1)
    return counted


# ------------------------------------------------------------------------ self-test

def self_test() -> int:
    """Replay the worked example: In Progress Sunday, flagged Thursday, unflagged
    Friday, back to Ready for Dev Monday, Validation Wednesday, Done Friday = 10."""
    tz = dt.timezone(dt.timedelta(hours=3))

    def at(day: int, hour: int = 9) -> dt.datetime:
        return dt.datetime(2026, 3, day, hour, tzinfo=tz)

    # Mar 1 2026 is a Sunday, so day N of the table == March N.
    events = [
        (at(1), "status", "In Progress"),
        (at(5), "flag", "Impediment"),
        (at(6), "flag", None),
        (at(9), "status", "Ready for Dev"),
        (at(11), "status", "Validation"),
        (at(13), "status", "Done"),
    ]
    end = dt.date(2026, 3, 13)
    counted = edct_days(events, end=end, count_done_day=True)
    skipped = edct_days(events, end=end, count_done_day=False)
    print(f"worked example, counting the Done day -> {counted} (definition says 10)")
    print(f"worked example, skipping the Done day  -> {skipped} (report-matching rule)")
    ok = counted == 10 and skipped == 9
    print("self-test passed" if ok else "SELF-TEST FAILED")
    return 0 if ok else 1


# ----------------------------------------------------------------------------- Jira

class Jira:
    def __init__(self) -> None:
        for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
            if not os.environ.get(k):
                sys.exit(f"FATAL: missing environment: {k}")
        self.base = os.environ["JIRA_BASE_URL"].rstrip("/")
        self.s = requests.Session()
        self.s.auth = (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
        self.s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        self.s.mount("https://", HTTPAdapter(max_retries=Retry(
            total=4, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"])))

    def search(self, jql: str, fields: list[str]) -> list[dict]:
        out, token = [], None
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

    def changelog(self, key: str) -> list[dict]:
        """One request per epic makes this the highest-volume caller, so a mid-response
        connection failure here is the likeliest way a whole run dies. Retry it."""
        out, start = [], 0
        while True:
            data = None
            for attempt in range(4):
                try:
                    r = self.s.get(f"{self.base}/rest/api/3/issue/{key}/changelog",
                                   params={"startAt": start, "maxResults": 100}, timeout=90)
                    r.raise_for_status()
                    data = r.json()
                    break
                except (requests.exceptions.ChunkedEncodingError,
                        requests.exceptions.ConnectionError,
                        urllib3.exceptions.ProtocolError) as e:
                    if attempt == 3:
                        raise RuntimeError(f"changelog {key} failed after 4 attempts: {e}")
                    time.sleep(2 ** attempt)
            out.extend(data.get("values", []))
            start += len(data.get("values", []))
            if start >= data.get("total", 0) or not data.get("values"):
                return out


def ts(s: str) -> dt.datetime:
    """Jira stamps look like 2026-04-30T20:21:56.849+0300 -- the offset has no colon,
    which datetime.fromisoformat rejects before Python 3.11."""
    s = s.strip().replace("Z", "+00:00")
    if len(s) > 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    return dt.datetime.fromisoformat(s)


def epic_events(histories: list[dict]) -> list[tuple[dt.datetime, str, str | None]]:
    events = []
    for h in histories:
        when = ts(h["created"])
        for it in h.get("items", []):
            if it.get("field") == "status":
                events.append((when, "status", it.get("toString")))
            elif it.get("field") == FLAG_FIELD:
                events.append((when, "flag", it.get("toString") or None))
    return events


def month_range(spec: str) -> list[tuple[int, int]]:
    lo, hi = spec.split(":")
    y1, m1 = (int(x) for x in lo.split("-"))
    y2, m2 = (int(x) for x in hi.split("-"))
    out = []
    while (y1, m1) <= (y2, m2):
        out.append((y1, m1))
        y1, m1 = (y1 + (m1 == 12), (m1 % 12) + 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="PMN")
    ap.add_argument("--months", default=None, help="YYYY-MM:YYYY-MM inclusive")
    ap.add_argument("--ai-only", action="store_true",
                    help="restrict to Implemented by AI Agent = Yes (the dashboard's view)")
    ap.add_argument("--team-any-clause", default=None, help="extra JQL to scope to one team")
    ap.add_argument("--out", default=None, help="write the monthly series as JSON")
    ap.add_argument("--per-epic", action="store_true", help="print every epic's days")
    ap.add_argument("--done-day", choices=["skip", "count"], default="skip",
                    help="'skip' (default) matches the R&D Efficiency report; 'count' "
                         "matches the written definition and reads ~1 day higher.")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.months:
        ap.error("--months is required unless --self-test")

    j = Jira()
    series: dict[str, dict] = {}
    for y, m in month_range(args.months):
        lo = dt.date(y, m, 1)
        hi = dt.date(y + (m == 12), (m % 12) + 1, 1)
        jql = (f'project = {args.project} AND issuetype = Epic AND status = Done '
               f'AND resolutiondate >= "{lo}" AND resolutiondate < "{hi}"')
        if args.ai_only:
            jql += f' AND {CF_IMPLEMENTED_BY_AI} = "Yes"'
        if args.team_any_clause:
            jql += f' AND {args.team_any_clause}'
        issues = j.search(jql, ["resolutiondate", CF_ISSUE_CATEGORY])

        vals, skipped = [], 0
        for i in issues:
            f = i["fields"]
            cat = f.get(CF_ISSUE_CATEGORY)
            cat = cat.get("value") if isinstance(cat, dict) else cat
            if cat == EXCLUDED_CATEGORY:
                skipped += 1
                continue
            resolved = ts(f["resolutiondate"]).date()
            days = edct_days(epic_events(j.changelog(i["key"])), end=resolved,
                             count_done_day=args.done_day == "count")
            vals.append(days)
            if args.per_epic:
                print(f"  {i['key']:12} {days:4}d  cat={cat}")
        avg = round(sum(vals) / len(vals)) if vals else None
        series[f"{y}/{m:02d}"] = {"avg": avg, "epics": len(vals), "maintenance_excluded": skipped}
        print(f"{y}/{m:02d}: avg {avg}  (n={len(vals)}, {skipped} Maintenance excluded)", flush=True)

    if args.out:
        json.dump(series, open(args.out, "w"), indent=2)
        print("wrote", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
