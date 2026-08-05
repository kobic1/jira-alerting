# PMN Release Status Email

Emails the PMN release-status KPI dashboard — 6 stat cards and 5 charts in one
self-contained HTML — to the PMN management distribution list **every Monday and
Thursday**, from GitHub Actions. PMN only.

Workflow: [`.github/workflows/pmn-release-status-email.yml`](../.github/workflows/pmn-release-status-email.yml)
· cron `0 5 * * 1,4` (05:00 UTC = 08:00 Israel; GitHub may delay scheduled runs a few hours)

## What is live, and what is not

| Section | Source | Refreshed every run? |
|---|---|---|
| Delivery — actual vs. expected epic burn-up | Jira | yes |
| Quality — open bugs trend | Jira | yes |
| % Epics developed by AI agents | Jira | yes |
| AI Fields Adoption | Jira | yes |
| **Epic Dev Cycle Time (EDCT)** | **Power BI snapshot** | **no — see below** |

EDCT lives in the "R&D Efficiency" Power BI app, behind interactive Microsoft SSO.
There is no headless route: the Power BI MCP connector does not expose that dataset
(checked 2026-08-05 — only 11 certified finance/HR datasets), and the report is
client-side rendered, so CI cannot scrape it. EDCT therefore comes from
`powerbi_snapshot.json`, and its chart caption always states the snapshot date and
says plainly that it was carried forward. EDCT is a monthly average, so a snapshot
one to three weeks old is still a fair number — it just must never be presented as
today's.

**Refresh the snapshot** from an interactive Claude session by running the
`pmn-kpi-dashboard` skill (it reads Power BI through the browser), then copying its
`report_values.json` into `powerbi_snapshot.json` and committing. Do it at least
monthly, and whenever a month closes.

## Nothing is sent unverified

`build_and_send.py` runs `verify_data.py` as a gate before a single chart is drawn.
It checks:

- **freshness** — every section carries today's `as_of` (a carried-forward section is
  a WARN, and the caption must say so);
- **current-month coverage** — the current month appears in every monthly series.
  Omitting it must be a stated decision (`omit_current_month`), never an accident;
- **reconciliation** — every EDCT and AI figure against the Power BI snapshot.

A FAIL fails the job and sends nothing. In CI the snapshot is older than the live
Jira data, so the run passes `--snapshot-stale-ok`: months at or after the snapshot
month reconcile as WARN, while **closed** months stay strict — a closed month that
drifts means the definition changed and is a hard failure.

This gate exists because on 2026-08-05 a dashboard went out with July AI epics at
20 of 26 (an `AGENTIC_AI_CODE` label count) while the report said 23 of 26, and with
August missing from both AI charts. Both defects were invisible in the finished page.

## The definitions that matter

AI work is the **`cf[15229]` "Implemented by AI Agent" field**, *not* the
`AGENTIC_AI_CODE` label. The label undercounts — July 2026: 20 by label, 23 by field,
and the report says 23. Label variants (`Agentic_AI_Code`, `AGENT_AI_CODE`) are not
the cause; they only ever co-occur with `AGENTIC_AI_CODE`.

AI Fields Adoption counts **all issue types and all statuses** by resolved date —
`cf[15229] = Yes` (marked) vs `cf[15262]` PR-URL populated (having metrics) — which
makes the monthly series sum to the report's quarterly "# Issues with PR ID"
(2026/Q2: 23 = 23 exactly). Narrowing it to Done Stories/Bugs understated July as
25 of 69 instead of 43 of 99.

## Running it by hand

Actions → **PMN Release Status Email** → *Run workflow*:

- `test-kobi-only` (default) — full build, emailed to Kobi alone, subject prefixed `[test]`
- `send-to-managers` — the real distribution
- `build-only` — build and verify, send nothing (artifact only)

Locally:

```bash
set -a && source .env && set +a
export POWER_AUTOMATE_EMAIL_URL='<the flow trigger URL>'
python3 pmn_dashboard/build_and_send.py --audience none        # build + verify only
python3 pmn_dashboard/build_and_send.py --audience test        # email yourself
```

## Secrets

Reuses the repo's existing `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, plus:

| Secret | Purpose |
|---|---|
| `POWER_AUTOMATE_EMAIL_URL` | The Outlook "Send an email V2" flow trigger. Carries its own `sig=` signature — a credential. **This repo is public**, so the URL exists only as a secret; `send_email.py` here has no baked-in default and refuses to send without it. |
| `PMN_DASHBOARD_RECIPIENTS` | The manager distribution list, semicolon-separated. Kept out of the repo so colleagues' addresses are not published. |
| `PMN_DASHBOARD_TEST_RECIPIENT` | Where `test-kobi-only` runs go. Defaults to Kobi if unset. |

## Release rollover

`config.json` holds the release number, its start/end dates, sprint boundaries, the
5-release history and `forecast_override`. When 26.4 closes, update that file — no
code change. `forecast_override` is the committed-scope forecast (73), used because
the 5-release regression over-predicts after the Bugs Bunnies and Guardians teams
left; set it to `null` to fall back to the regression.

## First send

`first_send_date` in `config.json` is `2026-08-10`. Manager sends before that date
exit cleanly without sending — the cron would otherwise have fired on Thu Aug 6,
and the distribution was asked to start Mon Aug 10. Remove the key (or set it to a
past date) once it has served its purpose.
