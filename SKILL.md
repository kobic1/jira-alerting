---
name: jira-alerting
description: >
  Run the Jira alerting system to send Teams notifications for production bugs and epic alerts.
  Use this skill whenever the user wants to run Jira alerts, send bug notifications, trigger the
  alerting pipeline, run a POC preview, check SLA breaches, or send Teams alerts for any Jira project.
  Trigger on phrases like "run alerts for", "send alerts", "run POC", "preview alerts",
  "run the alerting system", "check bugs for project X", or any mention of running the Jira alerting pipeline.
---

# Jira Alerting Skill

Run the Jira alerting pipeline for one or more Jira projects and deliver Teams notifications.

> Canonical copy of the jira-alerting skill, kept in-repo so it survives (the
> live skills-plugin copy is ephemeral). Keep this and the plugin copy in sync.

## Project location
```
/Users/Kobi.Cohen/jira-alerting
```

## Environment
Credentials are loaded from `/Users/Kobi.Cohen/jira-alerting/.env`:
- `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`
- `TEAMS_FLOW_URL` — Power Automate flow for Teams DMs (HTML digests)
- `SNOOZE_FLOW_URL` — (optional) Power Automate flow for the interactive Snooze card; see "Snooze" below

## Active rules (Live)
| Priority | Rule | Trigger |
|---|---|---|
| 1 | Production Bug — SLA Breached 🚨 | Bug SLA breached · CFB reporter |
| 2 | Production Bug — SLA Due Soon ⚠️ | Bug with 70%+ SLA consumed, not yet breached · CFB reporter |
| 3 | CFB — Regression Field is Empty | Bug with empty Regression field 24+ hrs |
| 3 | Production Bug — No Progress | Bug with no update 3+ days |
| 4 | Epics Awaiting PM Feedback | Epic in Validation, no PM comment for 2+ business days |
| 4 | Completed Epic, Status Not Done | Epic in Validation with no open work (all children Done, or none), unchanged 2+ business days → project lead |

## Running the pipeline

### Preview mode (sends only to Kobi, safe to test)
```bash
cd /Users/Kobi.Cohen/jira-alerting && \
set -a && source .env && set +a && \
TEAMS_FLOW_URL="$TEAMS_FLOW_URL" python3 main.py \
  --run-once \
  --preview-to kobi.cohen@nice.com \
  --project {PROJECT}
```

### Live mode (sends to real recipients)
```bash
cd /Users/Kobi.Cohen/jira-alerting && \
set -a && source .env && set +a && \
TEAMS_FLOW_URL="$TEAMS_FLOW_URL" python3 main.py \
  --run-once \
  --project {PROJECT}
```

### Multiple projects
Repeat `--project` for each one:
```bash
--project CXDV --project CXDVI
```

### Limit messages (useful for testing)
```bash
--max-digests 5   # cap total messages sent
```

### Sample mode (1 example per rule)
```bash
--sample-rules    # sends one example issue per active rule
```

## Step-by-step instructions

1. **Identify the project(s)** from the user's request (e.g. CXDV, CXDVI, PMN, PMSTND, WFM).
2. **Determine the mode**:
   - Default to **preview mode** unless the user explicitly asks to send to real recipients.
   - If the user says "POC", "preview", or "test" → use `--preview-to kobi.cohen@nice.com`.
   - If the user says "send for real", "live", or "go live" → omit `--preview-to`.
3. **Run the command** using the bash block above, substituting the project key(s).
4. **Report the results** — summarize how many cards were sent and which rules fired, using a table like:

| Rule | Matches |
|---|---|
| SLA Breached 🚨 | N issues |
| No Progress | N issues |
| ... | ... |

5. If 0 results for a project, explain that either no issues match the rule conditions today, or the project has no bugs reported by the CFB reporter.

## Snooze (interactive Adaptive Card) — DISABLED BY DEFAULT

The digest can be sent as a **Teams Adaptive Card** with a **⏰ Snooze 2h** button —
`Action.Submit`, so clicking it opens **no browser**, and ~2h later the alert
re-posts as a reminder. `MessageFormatter.format_digest_card()` builds it; the flow
(`SNOOZE_FLOW_URL`, "Post adaptive card and wait for a response → Delay 2h → Post
message") delivers it and handles the reminder. Verified working **for a preview to
yourself** (2026-07-16): no-browser button + a labeled 2h reminder.

**⚠️ It is OFF by default and must stay off until the flow is fixed.** The snooze
flow posts every card to a **fixed recipient** (Kobi), ignoring the per-message
`recipient` — so routing real multi-recipient traffic through it leaks everyone's
cards (and reminders) to that one person. Two guards enforce this:
- **Master switch:** `supports_cards` needs BOTH `SNOOZE_FLOW_URL` set AND
  `SNOOZE_CARDS_ENABLED=true`. The env flag is unset everywhere, so cards never fire.
- **Preview-only:** even if enabled, the dispatcher uses the card path only in
  preview mode (single reviewer). All live sends use the HTML flow.

So **all real delivery is HTML over `TEAMS_FLOW_URL`** (correctly routed per recipient).

**To send yourself one test card** (safe — preview to you only):
```bash
cd /Users/Kobi.Cohen/jira-alerting && set -a && source .env && set +a && \
SNOOZE_CARDS_ENABLED=true python3 main.py --run-once \
  --preview-to kobi.cohen@nice.com --sample-rules --max-digests 1 --project PMN
```

**Auth:** the snooze flow's powerplatform URL is OAuth — `send_card()` attaches a
bearer token from the cached MSAL token at `~/.jira_alerting_token.json`; if none, it
**falls back to the HTML digest** (never drops the alert). Seed the token once:
`python3 seed_snooze_token.py` (device-flow login). NOTE: `send()` must NOT attach a
bearer token to the SAS-signed `TEAMS_FLOW_URL` (`sig=`) — two auth schemes → HTTP 401
`DirectApiRequestHasMoreThanOneAuthorization`; it only attaches for non-SAS URLs.

**Reminder format:** the 2h re-post carries a "✅ Snoozed reminder — here's what you
snoozed:" banner and **no** per-issue Snooze links (you can't re-snooze a reminder).

**To re-enable for real recipients:** in Power Automate, bind the flow's "Post
adaptive card" step to `triggerBody()?['recipient']`, then set
`SNOOZE_CARDS_ENABLED=true`. The code already tags each card with only its owner
(per-user isolation is locked by a test), so it will route correctly once the flow
honors the recipient. Never edit that flow without Kobi's approval.

## Operational notes (2026-07-16)
- **Live scope**: alerts run for **PMN + PMSTND only**. `alerting.default_projects`
  (in `settings.yaml`) scopes a bare run so it never queries all Jira projects;
  `--project` overrides.
- **Exit code**: `main.py` returns non-zero when any digest fails to deliver
  (`groups_failed > 0`) — a broken run shows red instead of green.
- **Managerial once-a-day guard**: the summary skips a subscriber already sent today
  (state in `.managerial_cache.json`). `--managerial-force` re-sends; testing
  overrides bypass it. Caveat: ephemeral CI runners don't persist the cache.
- **Schedule**: GitHub Actions cron is `20 4 * * 1-5` (early + off-the-hour to
  absorb GitHub's 2–4h scheduling delay); production runs via Actions, not the
  in-process scheduler.
- `TEAMS_FLOW_URL` / `SNOOZE_FLOW_URL` load from `.env`. Preview bypasses dedup
  (safe to re-run). Per-rule cap is 10 groups.

## Demo ("demo <PROJECT>")
A full demo (all to Kobi via preview) has 4 parts and must show **all alert types**:
(1) per-person preview alerts (real data), (2) `[SAMPLE]` cards for types with no
live data (No Progress + both Epic Cycle Time rules), (3) management view, (4) the
PR-waiting alert (GitHub PRs pending review — a standalone `gh`-based script, not a
Jira rule). NOTE the project-name↔key-prefix gotcha: `project = CXCV` resolves to
issues keyed **CXDV**, so the management view must key by the actual prefix (resolve
it from the matches) or it shows 0.
