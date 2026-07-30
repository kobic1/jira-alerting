"""Entry point — wires all layers together and starts the scheduler.

Usage examples
──────────────
# Preview all digests — sends every card to YOU with a PREVIEW banner.
# Dedup is bypassed; nothing is marked as alerted.  Safe to run repeatedly.
python3 main.py --preview-to kobi.cohen@your-org.com --run-once

# Run for real once (after reviewing the preview)
python3 main.py --run-once

# Evaluate rules, print stats, send nothing at all
python3 main.py --dry-run --run-once

# Start the daily scheduler (cron: 0 9 * * * by default)
python3 main.py
"""

# Created by Kobi cohen
from __future__ import annotations

import argparse
import logging
import os
import sys

from src.config_loader import load_people, load_rules, load_settings
from src.delivery.deduplication import DeduplicationStore
from src.delivery.dispatcher import AlertDispatcher
from src.delivery.teams import TeamsGraphSender, TeamsPowerAutomateSender, TeamsWebhookSender
from src.ingestion.jira_client import JiraClient
from src.ingestion.people_registry import PeopleRegistry
from src.messaging.formatter import MessageFormatter
from src.messaging.managerial_summary import ManagerialSummaryReporter
from src.models import JiraUser
from src.rules.engine import RuleEngine
from src.scheduler.runner import AlertScheduler

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------

def build_sender(teams_cfg: dict):
    """Construct the configured Teams sender (Power Automate / Graph / Webhook)."""
    if teams_cfg.get("use_power_automate"):
        return TeamsPowerAutomateSender(
            flow_url=teams_cfg["power_automate"]["flow_url"],
            snooze_flow_url=os.environ.get("SNOOZE_FLOW_URL") or None,
            # OFF by default. The card/snooze path stays disabled until the
            # snooze flow routes by the per-message recipient (today it posts to
            # a fixed user, leaking everyone's cards + reminders to one person).
            # Set SNOOZE_CARDS_ENABLED=true only after that flow fix is verified.
            enable_cards=os.environ.get("SNOOZE_CARDS_ENABLED", "").lower() == "true",
        )
    if teams_cfg.get("use_graph_api"):
        graph = teams_cfg["graph_api"]
        return TeamsGraphSender(
            tenant_id=graph["tenant_id"],
            client_id=graph["client_id"],
            client_secret=graph["client_secret"],
            channel_id=graph["channel_id"],
        )
    return TeamsWebhookSender(webhook_url=teams_cfg["webhook_url"])


# ---------------------------------------------------------------------------
# Pipeline factory
# ---------------------------------------------------------------------------

def build_pipeline(
    settings: dict,
    rules_path: str,
    people_path: str = "config/people.yaml",
    preview_to_email: str | None = None,
    project_filter: list[str] | None = None,
    issue_type_filter: list[str] | None = None,
    max_digests: int | None = None,
    sample_rules: bool = False,
) -> callable:
    jira_cfg = settings["jira"]
    teams_cfg = settings["teams"]
    alerting_cfg = settings["alerting"]

    jira_client = JiraClient(
        base_url=jira_cfg["base_url"],
        email=jira_cfg["email"],
        api_token=jira_cfg["api_token"],
        timeout=int(jira_cfg.get("request_timeout_seconds", 30)),
    )

    sender = build_sender(teams_cfg)

    people_registry = load_people(people_path)

    # Resolve the preview recipient from the people registry (by email/name),
    # or build a minimal JiraUser if the email isn't in people.yaml.
    preview_recipient = _resolve_preview_recipient(
        preview_to_email
        or alerting_cfg.get("preview_to"),   # also settable in settings.yaml
        people_registry,
    )

    if preview_recipient:
        logger.warning(
            "PREVIEW MODE enabled — all digests will be sent to: %s (%s)",
            preview_recipient.display_name,
            preview_recipient.email,
        )

    # Optional snooze feature. Read straight from the environment (not settings.yaml)
    # so an unset var simply disables the feature instead of crashing config load.
    formatter = MessageFormatter(
        jira_client=jira_client,
        snooze_flow_url=os.environ.get("SNOOZE_FLOW_URL") or None,
    )
    dedup = DeduplicationStore(
        window_hours=int(alerting_cfg.get("deduplication_window_hours", 24)),
        store_path=alerting_cfg.get("deduplication_file", ".alert_cache.json"),
    )
    # Issue type filter: CLI overrides settings.yaml
    effective_type_filter = issue_type_filter or alerting_cfg.get("issue_types") or None

    # Project scope: CLI --project overrides; otherwise fall back to
    # alerting.default_projects. This is a safeguard so a bare run never fans out
    # to EVERY Jira project — live alerting stays limited to the configured set.
    if not project_filter:
        project_filter = alerting_cfg.get("default_projects") or None
        if project_filter:
            logger.info("No --project given; using default_projects: %s", project_filter)
        else:
            logger.warning(
                "No --project and no alerting.default_projects — rules will query ALL projects"
            )

    if project_filter:
        logger.info("Project filter active: %s", project_filter)
    if effective_type_filter:
        logger.info("Issue type filter active: %s", effective_type_filter)

    engine = RuleEngine(
        jira_client=jira_client,
        people_registry=people_registry,
        max_issues_per_rule=int(jira_cfg.get("max_results", 100)),
        project_filter=project_filter,
        issue_type_filter=effective_type_filter,
        project_fallback_recipients=alerting_cfg.get("project_fallback_recipients") or {},
    )
    dispatcher = AlertDispatcher(
        sender=sender,
        formatter=formatter,
        dedup=dedup,
        dry_run=alerting_cfg.get("dry_run", False),
        preview_recipient=preview_recipient,
        max_groups=max_digests,
        sample_rules=sample_rules,
    )

    def run_pipeline() -> dict:
        rules = load_rules(rules_path)
        min_sev = alerting_cfg.get("min_severity", "low")
        sev_ranks = {"high": 3, "medium": 2, "low": 1}
        min_rank = sev_ranks.get(min_sev, 1)
        active_rules = [
            r for r in rules
            if r.enabled and sev_ranks.get(r.severity.value, 1) >= min_rank
        ]
        groups = engine.evaluate_all(active_rules)
        return dispatcher.dispatch(groups)

    return run_pipeline


def build_managerial_reporter(
    settings: dict,
    rules_path: str,
    people_path: str = "config/people.yaml",
    override_recipient: str | None = None,
    force: bool = False,
    project_filter: list[str] | None = None,
) -> ManagerialSummaryReporter:
    """Construct the per-project managerial summary reporter from settings."""
    jira_cfg = settings["jira"]
    alerting_cfg = settings.get("alerting", {})
    ms_cfg = settings.get("managerial_summary", {})

    jira_client = JiraClient(
        base_url=jira_cfg["base_url"],
        email=jira_cfg["email"],
        api_token=jira_cfg["api_token"],
        timeout=int(jira_cfg.get("request_timeout_seconds", 30)),
    )
    people_registry = load_people(people_path)
    formatter = MessageFormatter(jira_client=jira_client)
    sender = build_sender(settings["teams"])

    subscribers_by_project = ms_cfg.get("subscribers_by_project", {}) or {}
    # CLI --project overrides the subscriber list; fall back to all subscribed projects.
    effective_projects = project_filter or sorted(subscribers_by_project.keys())

    engine = RuleEngine(
        jira_client=jira_client,
        people_registry=people_registry,
        max_issues_per_rule=int(jira_cfg.get("max_results", 100)),
        project_filter=effective_projects or None,
        issue_type_filter=alerting_cfg.get("issue_types") or None,
    )

    return ManagerialSummaryReporter(
        engine=engine,
        formatter=formatter,
        sender=sender,
        registry=people_registry,
        rules_path=rules_path,
        subscribers_by_project=subscribers_by_project,
        min_severity=ms_cfg.get("min_severity", alerting_cfg.get("min_severity", "low")),
        override_recipient=override_recipient,
        state_path=alerting_cfg.get("managerial_cache_file", ".managerial_cache.json"),
        force=force,
        project_filter=project_filter,
    )


def _resolve_preview_recipient(
    email_or_name: str | None,
    registry: PeopleRegistry,
) -> JiraUser | None:
    """Look up the preview recipient in the people registry, or build a bare JiraUser."""
    if not email_or_name:
        return None

    # Try matching against every person in the registry
    for person in registry._people:
        if (
            person.email.lower() == email_or_name.lower()
            or person.display_name.lower() == email_or_name.lower()
        ):
            return person.to_jira_user()

    # Not in registry — build a minimal JiraUser from the raw email/name
    logger.warning(
        "Preview recipient %r not found in people.yaml — "
        "add them to see their display name in the banner",
        email_or_name,
    )
    is_email = "@" in email_or_name
    return JiraUser(
        account_id=f"preview::{email_or_name}",
        display_name=email_or_name,
        email=email_or_name if is_email else None,
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    if fmt == "json":
        log_format = (
            '{"time":"%(asctime)s","level":"%(levelname)s",'
            '"logger":"%(name)s","msg":"%(message)s"}'
        )
    else:
        log_format = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=log_format,
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Jira alerting system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--rules",    default="config/rules.yaml")
    parser.add_argument("--people",   default="config/people.yaml")
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run the pipeline once and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate rules but send nothing",
    )
    parser.add_argument(
        "--preview-to",
        metavar="EMAIL",
        help=(
            "Preview mode: redirect all digests to this address with a PREVIEW banner. "
            "Dedup is bypassed; nothing is marked as alerted. "
            "Example: --preview-to kobi.cohen@your-org.com"
        ),
    )
    parser.add_argument(
        "--project",
        metavar="KEY",
        action="append",
        dest="projects",
        help=(
            "Restrict all rules to this Jira project key. "
            "Can be repeated for multiple projects. "
            "Example: --project PMN  or  --project PMN --project CX"
        ),
    )
    parser.add_argument(
        "--issue-type",
        metavar="TYPE",
        action="append",
        dest="issue_types",
        help=(
            "Override issue type filter from settings.yaml. "
            "Can be repeated. Example: --issue-type Bug --issue-type Story"
        ),
    )
    parser.add_argument(
        "--max-digests",
        metavar="N",
        type=int,
        default=None,
        help=(
            "Limit the number of digest messages sent (useful for test previews). "
            "Example: --max-digests 8"
        ),
    )
    parser.add_argument(
        "--sample-rules",
        action="store_true",
        help=(
            "Preview mode only: send exactly one message per active rule "
            "with a single example issue. Use to verify each rule looks correct "
            "before going live."
        ),
    )
    parser.add_argument(
        "--managerial-summary",
        action="store_true",
        help=(
            "Run the per-project managerial summary once and exit. Sends a combined "
            "rollup to every subscriber in settings.managerial_summary.subscribers_by_project."
        ),
    )
    parser.add_argument(
        "--managerial-summary-to",
        metavar="EMAIL",
        help=(
            "Like --managerial-summary, but redirect ALL summaries to this single "
            "address (for testing). Example: --managerial-summary-to kobi.cohen@nice.com"
        ),
    )
    parser.add_argument(
        "--managerial-force",
        action="store_true",
        help=(
            "Re-send the managerial summary even if a subscriber already received "
            "it today. Without this, a same-day re-run skips already-sent subscribers."
        ),
    )
    args = parser.parse_args()

    settings = load_settings(args.settings)
    log_cfg = settings.get("logging", {})
    configure_logging(level=log_cfg.get("level", "INFO"), fmt=log_cfg.get("format", "text"))

    if args.dry_run:
        settings.setdefault("alerting", {})["dry_run"] = True

    # Managerial summary: run once and exit (independent of the alert pipeline).
    if args.managerial_summary or args.managerial_summary_to:
        reporter = build_managerial_reporter(
            settings,
            rules_path=args.rules,
            people_path=args.people,
            override_recipient=args.managerial_summary_to,
            force=args.managerial_force,
            project_filter=args.projects or None,
        )
        stats = reporter.run()
        print(f"\nDone: {stats}")
        return 1 if stats.get("failed", 0) else 0

    pipeline = build_pipeline(
        settings,
        rules_path=args.rules,
        people_path=args.people,
        preview_to_email=args.preview_to,
        project_filter=args.projects,
        issue_type_filter=args.issue_types,
        max_digests=args.max_digests,
        sample_rules=args.sample_rules,
    )

    if args.run_once or args.preview_to:
        # --preview-to always implies --run-once (no point scheduling a preview)
        stats = pipeline()
        print(f"\nDone: {stats}")
        if args.preview_to and stats.get("groups_sent", 0) > 0:
            print(
                f"\n✅  {stats['groups_sent']} preview card(s) sent to {args.preview_to}.\n"
                f"   Review them in Teams, then run:\n"
                f"   python3 main.py --run-once\n"
                f"   to approve and deliver to real recipients."
            )
        # Surface delivery failures with a non-zero exit — otherwise a scheduled
        # run that silently failed to deliver every digest still shows green.
        if stats.get("groups_failed", 0) > 0:
            logger.error(
                "%d group(s) failed to deliver — exiting non-zero",
                stats["groups_failed"],
            )
            return 1
        return 0

    sched_cfg = settings.get("scheduler", {})
    default_cron = sched_cfg.get("cron_expression", "0 9 * * *")
    default_tz = sched_cfg.get("timezone", "UTC")
    scheduler = AlertScheduler(
        pipeline_fn=pipeline,
        cron_expression=default_cron,
        timezone=default_tz,
    )

    # Register the daily managerial summary as its own job when enabled.
    ms_cfg = settings.get("managerial_summary", {})
    if ms_cfg.get("enabled"):
        reporter = build_managerial_reporter(settings, rules_path=args.rules, people_path=args.people)
        scheduler.add_job(
            reporter.run,
            cron_expression=ms_cfg.get("cron_expression", default_cron),
            job_id="managerial_summary",
            name="Managerial Summary",
            timezone=ms_cfg.get("timezone", default_tz),
        )

    scheduler.start()


if __name__ == "__main__":
    sys.exit(main())
