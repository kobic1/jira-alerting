# Jira Alerting System

A modular, rule-driven alerting system that queries Jira via REST API, evaluates configurable rules, and delivers personalized alerts to Microsoft Teams.

## Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│   Data Ingestion │ -> │  Rule Evaluation  │ -> │ Message Generation│ -> │    Delivery      │
│   (JiraClient)  │    │  (RuleEngine)     │    │ (MessageFormatter)│    │  (Dispatcher)    │
└─────────────────┘    └──────────────────┘    └──────────────────┘    └──────────────────┘
         ↑                      ↑                                                ↑
  config/settings.yaml   config/rules.yaml                              DeduplicationStore
```

```
jira-alerting/
├── main.py                      # Entry point and wiring
├── config/
│   ├── settings.yaml            # Jira/Teams credentials, scheduler config
│   └── rules.yaml               # Alert rules (add new rules here)
├── src/
│   ├── models.py                # Shared data classes (JiraIssue, RuleMatch, AlertGroup…)
│   ├── config_loader.py         # YAML + env-var resolution
│   ├── ingestion/
│   │   └── jira_client.py       # Jira REST API, pagination, changelog enrichment
│   ├── rules/
│   │   ├── conditions.py        # Operator functions (gt, lt, is_null, in, …)
│   │   └── engine.py            # Rule evaluation loop, grouping by owner
│   ├── messaging/
│   │   └── formatter.py         # Adaptive Card builder (per-user & channel summary)
│   ├── delivery/
│   │   ├── deduplication.py     # File-based alert cache with TTL window
│   │   ├── teams.py             # Webhook sender + Graph API sender (DM support)
│   │   └── dispatcher.py        # Orchestrates dedup → format → send
│   └── scheduler/
│       └── runner.py            # APScheduler cron wrapper
└── tests/
    ├── test_conditions.py
    ├── test_engine.py
    └── test_deduplication.py
```

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set environment variables
export JIRA_BASE_URL=https://your-org.atlassian.net
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=your_api_token
export TEAMS_WEBHOOK_URL=https://your-org.webhook.office.com/...

# 3. Test with a single dry run (no messages sent)
python3 main.py --run-once --dry-run

# 4. Run for real once
python3 main.py --run-once

# 5. Start the daily scheduler (cron: 0 9 * * * by default)
python3 main.py
```

## Adding a New Rule

Edit `config/rules.yaml` and add an entry — no code changes needed:

```yaml
- id: my_new_rule
  enabled: true
  name: "My Custom Alert"
  description: "Issues blocked for too long"
  jql: 'status = "Blocked" AND updated <= -3d'
  jira_filter_id: null          # Optional: ID of a saved Jira filter
  conditions:
    - field: days_since_update
      operator: gt
      value: 3
  group_by: assignee            # "assignee" or "reporter"
  severity: high                # high | medium | low
  message_template: |
    *Blocked for {days_since_update} days* (threshold: {threshold})
```

### Available Condition Fields

| Field | Description |
|---|---|
| `age_days` | Days since issue was created |
| `days_since_update` | Days since last update |
| `cycle_time_days` | Days since first "In Progress" transition |
| `assignee` | Assignee object (use `is_null` / `is_not_null`) |
| `reporter` | Reporter object |
| `status` | Current status string |
| `priority` | Priority string (High, Medium, Low…) |
| `issue_type` | Issue type string |

### Available Operators

`gt`, `gte`, `lt`, `lte`, `eq`, `neq`, `is_null`, `is_not_null`, `contains`, `in`, `not_in`

## Delivery Modes

Set `alerting.delivery_mode` in `config/settings.yaml`:

| Mode | Behavior |
|---|---|
| `per_user` | One Teams message per owner, listing all their alerts |
| `channel_summary` | One message to the channel with all alerts |
| `per_rule` | One message per rule, grouped by owner |

When `use_graph_api: true` and `delivery_mode: per_user`, alerts are sent as direct messages to each user's Teams account using their Jira `accountId`.

## Deduplication

The system tracks which `(rule_id, issue_key)` pairs have been alerted. If the same issue matches the same rule within `deduplication_window_hours` (default: 24h), it is skipped. The cache is stored in `.alert_cache.json`.

## Running Tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```
