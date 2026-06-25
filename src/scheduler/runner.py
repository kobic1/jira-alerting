"""Scheduler — runs one or more pipelines on independent cron schedules."""
from __future__ import annotations

import logging
import signal
import sys
from dataclasses import dataclass
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


@dataclass
class _Job:
    fn: callable
    cron: str
    job_id: str
    name: str
    timezone: str


class AlertScheduler:
    def __init__(
        self,
        pipeline_fn: callable | None = None,
        cron_expression: str | None = None,
        timezone: str = "UTC",
    ):
        self._timezone = timezone
        self._scheduler = BlockingScheduler(timezone=timezone)
        self._jobs: list[_Job] = []
        # Backward-compatible: register the primary pipeline if given.
        if pipeline_fn and cron_expression:
            self.add_job(pipeline_fn, cron_expression, "jira_alerting", "Jira Alert Pipeline")

    def add_job(
        self,
        fn: callable,
        cron_expression: str,
        job_id: str,
        name: str,
        timezone: str | None = None,
    ) -> None:
        self._jobs.append(
            _Job(fn=fn, cron=cron_expression, job_id=job_id, name=name, timezone=timezone or self._timezone)
        )

    def start(self) -> None:
        if not self._jobs:
            logger.error("No jobs registered — nothing to schedule")
            return

        for job in self._jobs:
            trigger = CronTrigger.from_crontab(job.cron, timezone=job.timezone)
            self._scheduler.add_job(
                self._run_safe,
                trigger=trigger,
                id=job.job_id,
                name=job.name,
                args=[job.fn, job.name],
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        for scheduled in self._scheduler.get_jobs():
            logger.info(
                "Scheduled '%s' (id=%s) | Next run: %s",
                scheduled.name,
                scheduled.id,
                scheduled.next_run_time,
            )
        self._scheduler.start()

    def _run_safe(self, fn: callable, name: str) -> None:
        start = datetime.utcnow()
        logger.info("'%s' run starting at %s", name, start.isoformat())
        try:
            stats = fn()
            elapsed = (datetime.utcnow() - start).total_seconds()
            logger.info("'%s' completed in %.1fs | stats=%s", name, elapsed, stats)
        except Exception:
            logger.exception("'%s' run failed", name)

    def _shutdown(self, signum, frame) -> None:
        logger.info("Shutdown signal received — stopping scheduler")
        self._scheduler.shutdown(wait=False)
        sys.exit(0)
