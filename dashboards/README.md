# Release Status Emails

Emails the release-status KPI dashboards — stat cards and charts in one
self-contained HTML — to their management distribution lists **every Monday and
Thursday**, from GitHub Actions. Nobody's laptop is involved.

| Project | Config | Recipients secret | Sections |
|---|---|---|---|
| PMN (Performance Management Native) | `config_pmn.json` | `PMN_DASHBOARD_RECIPIENTS` | Delivery, EDCT, Quality, % Epics by AI, AI Fields Adoption |
| CXCO (CXone Coaching) | `config_cxco.json` | `CXCO_DASHBOARD_RECIPIENTS` | Delivery, EDCT, Quality, % Epics by AI |

Workflow: [`.github/workflows/release-status-emails.yml`](../.github/workflows/release-status-emails.yml)
· cron `0 5 * * 1,4` (05:00 UTC = 08:00 Israel; GitHub may delay scheduled runs a few
hours) · the two projects run as independent matrix jobs, so one failing does not
stop the other.

Adding a third project is a config file, a Power BI snapshot, and a recipients
secret — no code change.

## What is live, and what is not

Delivery, Quality and the AI-adoption sections come straight from the Jira REST API
on every run. **EDCT does not.** It lives in the "R&D Efficiency" Power BI app behind
interactive Microsoft SSO, and there is no headless route: the Power BI MCP connector
does not expose that dataset (checked 2026-08-05 — only 11 certified finance/HR
datasets), and the report is client-side rendered, so CI cannot scrape it.

EDCT therefore comes from `powerbi_snapshot*.json`, and its caption states the
snapshot date and says plainly that it was carried forward. EDCT is a monthly
average, so a snapshot one to three weeks old is still a fair number — it just must
never be presented as today's.

**Refresh the snapshots** from an interactive Claude session (the `pmn-kpi-dashboard`
skill reads Power BI through the browser), then commit. At least monthly, and
whenever a month closes. Each snapshot file documents the exact filters to set.

## Nothing is sent unverified

`build_and_send.py` runs `verify_data.py` before a single chart is drawn:

- **freshness** — every section carries today's `as_of` (carried-forward is a WARN,
  and the caption must say so);
- **current-month coverage** — the current month appears in every monthly series.
  Omitting it must be a stated decision (`omit_current_month`), never an accident;
- **stated omissions** — a whole section may be left out only if the data says why
  (`omitted_sections`), which is how CXCO omits AI Fields Adoption;
- **reconciliation** — every EDCT and AI figure against the Power BI snapshot.

A FAIL fails the job and sends nothing. In CI the snapshot is older than live Jira,
so the run passes `--snapshot-stale-ok`: months at or after the snapshot month
reconcile as WARN, while **closed** months stay strict — a closed month that drifts
means the definition changed and is a hard failure.

This gate exists because on 2026-08-05 a PMN dashboard went out with July AI epics at
20 of 26 (an `AGENTIC_AI_CODE` label count) while the report said 23 of 26, and with
August missing from both AI charts. Rebuilding CXCO the next day found the same
defect there: 15 of 27 shown, 18 of 27 actual.

## The definitions that matter

AI work is the **`cf[15229]` "Implemented by AI Agent" field**, *not* the
`AGENTIC_AI_CODE` label. The label undercounts (PMN July: 20 by label, 23 by field;
CXCO July: 15 by label, 18 by field). Label variants (`Agentic_AI_Code`,
`AGENT_AI_CODE`) are not the cause; they only ever co-occur with `AGENTIC_AI_CODE`.

AI Fields Adoption counts **all issue types and all statuses** by resolved date —
`cf[15229] = Yes` (marked) vs `cf[15262]` PR-URL populated (having metrics) — which
makes the monthly series sum to the report's quarterly "# Issues with PR ID"
(2026/Q2: 23 = 23 exactly). Narrowing it to Done Stories/Bugs understated PMN's July
as 25 of 69 instead of 43 of 99.

## Running it by hand

Actions → **Release Status Emails** → *Run workflow*, choosing an audience
(`test-kobi-only` default / `send-to-managers` / `build-only`) and a project
(`both` / `pmn` / `cxco`).

Locally:

```bash
set -a && source .env && set +a
export POWER_AUTOMATE_EMAIL_URL='<the flow trigger URL>'
python3 dashboards/build_and_send.py --config config_cxco.json --audience none   # build + verify only
python3 dashboards/build_and_send.py --config config_pmn.json  --audience test   # email yourself
```

## Secrets

Reuses the repo's existing `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, plus:

| Secret | Purpose |
|---|---|
| `POWER_AUTOMATE_EMAIL_URL` | The Outlook "Send an email V2" flow trigger. Carries its own `sig=` signature — a credential. **This repo is public**, so the URL exists only as a secret; `send_email.py` here has no baked-in default and refuses to send without it. |
| `PMN_DASHBOARD_RECIPIENTS` | PMN distribution list, semicolon-separated. |
| `CXCO_DASHBOARD_RECIPIENTS` | CXCO distribution list, semicolon-separated. |
| `PMN_DASHBOARD_TEST_RECIPIENT` | Where `test-kobi-only` runs go, for both projects. Defaults to Kobi if unset. |

Recipient addresses are kept in secrets, not the repo, so colleagues' addresses are
not published. A `send-to-managers` run fails fast if the flow URL or the relevant
recipients secret is missing, rather than building for two minutes and then dropping
the email.

## Release rollover

Each config holds the release number, start/end dates, sprint boundaries, the
5-release history and `forecast_override`. When 26.4 closes, update the configs — no
code change. `forecast_override` replaces the 5-release regression where that
regression misfits: PMN 73 (capacity lost when the Bugs Bunnies and Guardians teams
left), CXCO 43 (committed scope — CXCO ramped from 3/1/2/11/35, so a straight-line
fit under-predicts). Set it to `null` to fall back to the regression.

## First send

`first_send_date` is `2026-08-10` in both configs. Manager sends before that date
exit cleanly — the cron would otherwise have fired Thu Aug 6, and the distributions
were asked to start Mon Aug 10. Remove the key once it has served its purpose.
