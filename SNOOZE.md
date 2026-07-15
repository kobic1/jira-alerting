# Snooze button (per-issue "remind me in 2 hours")

Each issue row in a daily digest can carry a **⏰ Snooze 2h** link. Clicking it
asks a Power Automate flow to wait 2 hours and then re-post a reminder about
that one issue to the same person.

## Why it works this way

The alerting job is a **stateless batch script** (GitHub Actions runs it once,
then the runner is gone). Nothing of ours stays alive to catch a click or fire
a timer later. And digests are posted to Teams as **HTML by the Flow bot**, and
HTML messages can only hold hyperlinks — not native call-back buttons.

So the snooze is a **hyperlink → a second Power Automate flow** whose built-in
`Delay` action holds the 2-hour timer for us. Power Automate keeps the state; we
stay serverless. All context the reminder needs (issue key, summary, Jira URL,
recipient) is carried in the link's query string, so the flow never has to call
back into Jira.

Clicking the link opens a browser tab that shows the flow's response
("Snoozed ✓"). That's expected — a true in-Teams button would require Adaptive
Cards and a larger rework.

## Feature flag

The link only appears when the `SNOOZE_FLOW_URL` environment variable is set. If
it's unset, digests render exactly as before. It is read straight from the
environment (not `settings.yaml`) so an unset value silently disables the
feature instead of failing config load.

## One-time Power Automate setup (~5 min)

1. https://make.powerautomate.com → **Create** → **Instant cloud flow** →
   **Skip** (start blank).
2. Trigger: **When an HTTP request is received**.
   - Set **Method** = `GET` (gear icon → Show advanced → Method).
   - No JSON schema needed; parameters arrive as query-string values.
3. Add action: **Delay** (Schedule connector) → Count `2`, Unit `Hour`.
4. Add action: **Microsoft Teams → Post message in a chat or channel**.
   - Post as **Flow bot**, Post in **Chat with Flow bot**.
   - **Recipient** (expression): `triggerOutputs()?['queries']?['recipient']`
   - **Message** (expression), building an HTML reminder from the query params:
     ```
     concat(
       '⏰ <b>Snooze reminder</b><br/>',
       '<a href="', triggerOutputs()?['queries']?['url'], '"><b>',
       triggerOutputs()?['queries']?['issue'], '</b></a> — ',
       triggerOutputs()?['queries']?['summary']
     )
     ```
5. (Optional) Add a final **Response** action returning HTML
   `⏰ Snoozed — you'll get a reminder in 2 hours.` so the browser tab shows a
   friendly confirmation.
6. **Save**, copy the trigger's **HTTP GET URL**, and register it as
   `SNOOZE_FLOW_URL`:
   - Local: `export SNOOZE_FLOW_URL="<url>"`
   - GitHub Actions: repo → Settings → Secrets and variables → Actions →
     **New repository secret** named `SNOOZE_FLOW_URL`. (The workflow already
     passes it through to both steps.)

## Verify

Run a preview once the secret exists — every issue row should end with a
**⏰ Snooze 2h** link:

```
python3 main.py --preview-to <you>@nice.com --run-once --project PMN
```
