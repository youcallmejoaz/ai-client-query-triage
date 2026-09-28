"""In-process scheduling: poll the inbox every POLL_SECONDS and post the daily summary at DIGEST_TIME.

Turn it off (SCHEDULER_ENABLED=false) when cron or n8n drives `triage poll` / `triage digest --send`
or the /api/poll and /api/digest endpoints instead.
"""

from __future__ import annotations

import logging
from typing import Any

from .digest import build_digest, digest_message
from .pipeline import Services, poll_safely

log = logging.getLogger(__name__)


def run_poll(svc: Services) -> None:
    if svc.mail is None:  # not connected yet: the job starts working as soon as it is
        return
    report, _ = poll_safely(svc)  # logs and records failures itself
    if report is not None and (report.processed or report.closed_by_reply):
        log.info("Poll: %s", report.as_dict())


def run_digest(svc: Services) -> None:
    try:
        svc.notifier.send(digest_message(build_digest(svc.db, svc.clock()), svc.settings))
    except Exception:
        log.exception("Scheduled digest failed")


def start_scheduler(svc: Services) -> Any:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    settings = svc.settings
    hour, minute = (int(part) for part in settings.digest_time.split(":"))
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    if settings.mail_provider != "demo" or svc.mail is not None:
        scheduler.add_job(
            run_poll,
            "interval",
            seconds=settings.poll_seconds,
            args=[svc],
            id="poll",
            max_instances=1,
            coalesce=True,
        )
    scheduler.add_job(
        run_digest,
        CronTrigger(day_of_week=settings.digest_days, hour=hour, minute=minute, timezone=settings.timezone),
        args=[svc],
        id="digest",
    )
    scheduler.start()
    log.info(
        "Scheduler started: poll every %ss, digest %s at %s",
        settings.poll_seconds,
        settings.digest_days,
        settings.digest_time,
    )
    return scheduler
