"""APScheduler-based job scheduler — trading tasks only."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, List, Optional

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from backend.config import get_settings
from backend.utils.logger import get_logger

logger = get_logger("backend.services.scheduler_service")


class SchedulerService:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._scheduler: Optional[AsyncIOScheduler] = None

    def _build_scheduler(self) -> AsyncIOScheduler:
        db_url = self._settings.database_url
        jobstore = SQLAlchemyJobStore(url=db_url)
        executor = AsyncIOExecutor()
        scheduler = AsyncIOScheduler(
            jobstores={"default": jobstore},
            executors={"default": executor},
            timezone=self._settings.scheduler_timezone,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 300,
            },
        )
        return scheduler

    def start(self) -> None:
        if self._scheduler and self._scheduler.running:
            return
        self._scheduler = self._build_scheduler()
        self._register_all_jobs()
        self._scheduler.start()
        logger.info("Scheduler started (timezone=%s)", self._settings.scheduler_timezone)

    def shutdown(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    def _register_all_jobs(self) -> None:
        if not self._settings.enable_scheduler:
            logger.info("Scheduler disabled via ENABLE_SCHEDULER=false")
            return

        from backend.tasks.task_trading_agent import run_trading_agent_task
        from backend.tasks.task_paper_monitor import run_paper_monitor
        from backend.tasks.task_balance_snapshot import run_balance_snapshot
        from backend.tasks.task_live_monitor import run_live_monitor

        tz = self._settings.scheduler_timezone

        self._add_cron_job(
            func=run_trading_agent_task,
            job_id="trading_agent",
            kwargs={},
            minute="*/15", timezone=tz,
        )
        self._add_cron_job(
            func=run_paper_monitor,
            job_id="paper_monitor",
            kwargs={},
            minute="*/5", timezone=tz,
        )
        self._add_cron_job(
            func=run_balance_snapshot,
            job_id="balance_snapshot",
            kwargs={},
            minute="*", timezone=tz,
        )
        self._add_cron_job(
            func=run_live_monitor,
            job_id="live_monitor",
            kwargs={},
            minute="*/5", timezone=tz,
        )

        logger.info("Registered 4 trading jobs")

    def _add_cron_job(self, func: Callable, job_id: str, kwargs: dict, **cron_kwargs) -> None:
        trigger = CronTrigger(**cron_kwargs)
        self._scheduler.add_job(
            func,
            trigger=trigger,
            id=job_id,
            kwargs=kwargs,
            replace_existing=True,
        )
        logger.debug("Registered job: %s", job_id)

    def trigger_job(self, job_id: str) -> bool:
        try:
            self._scheduler.modify_job(job_id, next_run_time=datetime.now(timezone.utc))
            return True
        except Exception as exc:
            logger.error("trigger_job %s: %s", job_id, exc)
            return False

    def list_jobs(self) -> List[dict]:
        if not self._scheduler:
            return []
        return [
            {
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "is_paused": job.next_run_time is None,
            }
            for job in self._scheduler.get_jobs()
        ]


_scheduler_service: SchedulerService | None = None


def get_scheduler_service() -> SchedulerService:
    global _scheduler_service
    if _scheduler_service is None:
        _scheduler_service = SchedulerService()
    return _scheduler_service
