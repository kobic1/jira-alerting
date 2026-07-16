# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A rule-driven pipeline that queries Jira, evaluates configurable rules, and delivers
personalized Microsoft Teams DMs (bug/epic alerts). Rules live in YAML — most changes
are config, not code.

## Commands

```bash
# Install
pip install -r requirements.txt          # runtime
pip install -r requirements-dev.txt      # + pytest for tests

# Credentials come from .env — source it before any run:
set -a && source .env && set +a

# Dry run (evaluate + log what would send, no messages)
python3 main.py --run-once --dry-run

# Preview (send ALL digests to yourself only; dedup bypassed — safe to re-run)
python3 main.py --run-once --preview-to kobi.cohen@nice.com --project PMN --project PMSTND

# One example issue per rule (great for testing formatting)
python3 main.py --run-once --preview-to kobi.cohen@nice.com --sample-rules --project PMN

# Live send to real recipients (omit --preview-to)
python3 main.py --run-once --project PMN --project PMSTND

# Managerial summary — per-project rollup to subscribers (read-only, ignores dedup)
python3 main.py --run-once --managerial-summary --managerial-summary-to kobi.cohen@nice.com

# Scheduler daemon (APScheduler; omit --run-once). Production actually runs via
# GitHub Actions cron, not this — see .github/workflows/daily-alerts.yml
python3 main.py

# Tests
pytest tests/ -v
pytest tests/test_formatter.py -v                                  # one file
pytest tests/test_formatter.py::test_message_is_html_string -v     # one test
```

Note: `tests/test_engine.py` has pre-existing failures (a fixture passes
`RuleConfig(enabled=…)`, which the model no longer accepts). Unrelated to delivery/
formatter work — don't treat them as regressions you introduced.

## Architecture

Four stages, wired together in `main.py::build_pipeline`:

```
JiraClient (ingestion) → RuleEngine (rules) → MessageFormatter (messaging) → AlertDispatcher (delivery)
     settings.yaml           rules.yaml                                            DeduplicationStore
```

- **Ingestion** (`src/ingestion/`): `jira_client.py` runs each rule's JQL, paginates,
  and enriches issues with changelog-derived fields (cycle time, days since update).
  `people_registry.py` loads `config/people.yaml` (roles: manager, engineering_manager,
  product_manager) used for role-based routing.
- **Rules** (`src/rules/`): `engine.py` evaluates every rule and groups matches into
  `AlertGroup`s (one per recipient). `conditions.py` holds the operator functions.
  Rules are **data**, defined in `config/rules.yaml` — adding a rule needs no code.
- **Messaging** (`src/messaging/`): `formatter.py` builds two outputs from the same
  data — `format_digest()` (rich HTML) and `format_digest_card()` (Adaptive Card with
  the no-browser Snooze button). `managerial_summary.py` builds the per-project rollup.
- **Delivery** (`src/delivery/`): `dispatcher.py` orchestrates dedup → format → send.
  `teams.py` has three senders; `deduplication.py` is a file-backed `(rule_id,
  issue_key)` cache with a TTL window (`.alert_cache.json`).

### Rule model (config/rules.yaml)

- `status: Live | POC | Disabled` (legacy `enabled: bool` still maps: true→Live,
  false→Disabled). **Preview mode shows Live + POC; live mode sends only Live rules.**
- `conditions` use operators from `conditions.py` (`gt`, `lt`, `is_null`, `in`, …).
  Two operators are **role-aware** (e.g. `days_since_role_comment`) and are evaluated
  on a separate path in the engine, not through the generic operator table.
- `group_by: assignee | reporter | notify_role`. With `notify_roles: [...]` the rule
  **fans out** to every person holding those roles (from people.yaml). `group_by`
  auto-infers to `notify_role` when `notify_roles` is present.
- `owner_override` + `fallback_assignee_role: project_lead` resolves the recipient live
  from Jira's project lead (cached per project per run) when an issue is unassigned.
- Production-bug rules are scoped in JQL to the **CFB reporter** account; "0 results"
  usually means nothing matched today, not a bug.

### Configuration & secrets

`config/settings.yaml` uses `${ENV_VAR}` placeholders resolved by
`config_loader.py::_resolve_env` (raises if a referenced var is unset). Values come
from `.env`. Required: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `TEAMS_FLOW_URL`.
Optional: `SNOOZE_FLOW_URL` (enables card delivery). The CLI overrides settings for
project/issue-type/preview filters.

### Delivery paths (teams.py)

`build_sender` picks one sender from `settings.teams`:
- **Power Automate** (default, `use_power_automate: true`): one flow, recipient passed
  per-message so each person gets their own DM.
- **Graph API** / **Webhook**: alternate senders (channel or Azure-app DMs).

The **Power Automate sender has two send paths**:
- `send()` → posts `{recipient, message}` (HTML) to `TEAMS_FLOW_URL`.
- `send_card()` → posts `{recipient, card, message}` to `SNOOZE_FLOW_URL` when
  `supports_cards` is true (i.e. `SNOOZE_FLOW_URL` is set). The dispatcher routes to
  the card path automatically; otherwise it falls back to the HTML digest.

**Snooze auth:** the snooze flow's trigger is OAuth (powerplatform direct-API URL, no
SAS `sig=`), so `send_card()` attaches an Azure AD bearer token from the MSAL cache at
`~/.jira_alerting_token.json` and **refuses to send without one** (a plain POST 401s).
The runtime only refreshes silently — seed the cache once with an interactive login:
`python3 seed_snooze_token.py` (device flow; sign in at microsoft.com/devicelogin).

### Preview vs live vs dry-run (dispatcher.py)

- **Preview** (`--preview-to`): redirects every digest to one reviewer, **bypasses
  dedup**, prepends a PREVIEW banner, and shows both Live and POC rules. Never writes
  the dedup store. `--preview-to` implies `--run-once`.
- **Live**: filters to Live rules, applies dedup, marks issues alerted on success.
- **Dry-run** (`--dry-run`): logs intended sends, delivers nothing.
- **Managerial summary**: independent, read-only path — never touches the dedup store,
  so it always reflects current state and can't interfere with the alert pipeline.

### Production scheduling

Production runs via **GitHub Actions** (`.github/workflows/daily-alerts.yml`), not the
in-process APScheduler: weekdays at 07:00 UTC (10:00 Israel), live send for PMN+PMSTND
plus the managerial summary. Secrets are provided as repo Actions secrets. The
APScheduler daemon (`src/scheduler/runner.py`) supports multiple independent cron jobs
and is used for local long-running mode.
