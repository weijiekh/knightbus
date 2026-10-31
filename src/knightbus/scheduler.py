import logging
import time
from datetime import date, datetime, time as time_of_day, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from knightbus.collector import GTFSCollector
from knightbus.config import Settings, get_settings
from knightbus.db import Base, SessionLocal, engine
from knightbus.etl import ETLService
from knightbus.logging import configure_logging
from knightbus.migrations import apply_migrations
from knightbus.models import StaticFeedState
from knightbus.training import TrainingService

logger = logging.getLogger(__name__)


def is_local_time_in_window(
    current: datetime,
    start_local: time_of_day,
    end_local: time_of_day,
) -> bool:
    current_local_time = current.timetz().replace(tzinfo=None)
    if start_local == end_local:
        return True
    if start_local < end_local:
        return start_local <= current_local_time < end_local
    return current_local_time >= start_local or current_local_time < end_local


def should_run_realtime_poll(*, active_hours: bool, now: datetime | None = None, settings: Settings | None = None) -> bool:
    resolved_settings = settings or get_settings()
    current = now or datetime.now(resolved_settings.gtfs_service_tzinfo)
    if current.tzinfo is None:
        current = current.replace(tzinfo=resolved_settings.gtfs_service_tzinfo)
    else:
        current = current.astimezone(resolved_settings.gtfs_service_tzinfo)

    in_active_window = is_local_time_in_window(
        current,
        resolved_settings.gtfs_active_poll_start_time,
        resolved_settings.gtfs_active_poll_end_time,
    )
    return in_active_window if active_hours else not in_active_window


def poll_realtime(*, active_hours: bool, poll_label: str) -> None:
    settings = get_settings()
    if not should_run_realtime_poll(active_hours=active_hours, settings=settings):
        logger.info("skipping realtime poll because current local time does not match this poll window", extra={"poll_label": poll_label})
        return

    for feed in settings.resolved_feeds:
        collector = GTFSCollector(feed)
        with SessionLocal() as db:
            vehicle_payload = collector.fetch_vehicle_positions()
            if vehicle_payload:
                inserted = collector.normalize_vehicle_positions(db, vehicle_payload)
                logger.info("stored vehicle positions", extra={"feed": feed.name, "inserted": inserted, "poll_label": poll_label})
            else:
                logger.info("skipping vehicle position poll because no URL is configured", extra={"feed": feed.name, "poll_label": poll_label})

            trip_payload = collector.fetch_trip_updates()
            if trip_payload:
                inserted = collector.normalize_trip_updates(db, trip_payload)
                logger.info("stored trip updates", extra={"feed": feed.name, "inserted": inserted, "poll_label": poll_label})
            else:
                logger.info("skipping trip updates poll because no URL is configured", extra={"feed": feed.name, "poll_label": poll_label})


def poll_realtime_active_hours() -> None:
    poll_realtime(active_hours=True, poll_label="active_hours")


def poll_realtime_off_hours() -> None:
    poll_realtime(active_hours=False, poll_label="off_hours")


def refresh_static(*, only_missing: bool = False) -> None:
    settings = get_settings()
    for feed in settings.resolved_feeds:
        collector = GTFSCollector(feed)
        with SessionLocal() as db:
            # Startup runs at any hour, so skip the multi-MB download unless the
            # feed was never loaded; the nightly maintenance job does the real check.
            if only_missing and db.get(StaticFeedState, feed.name) is not None:
                continue
            payload = collector.fetch_static()
            if not payload:
                continue
            if collector.static_unchanged(db, payload):
                logger.info("GTFS static feed unchanged, skipping", extra={"feed": feed.name})
                del payload
                continue
            collector.load_static_feed(db, payload)
            logger.info("refreshed GTFS static feed", extra={"feed": feed.name})
            del payload  # release the zip before the next feed downloads


def refresh_features() -> None:
    with SessionLocal() as db:
        count = ETLService().refresh_segment_statistics(db)
        logger.info("refreshed segment statistics", extra={"updated": count})


def run_training() -> None:
    with SessionLocal() as db:
        version = TrainingService().run_training(db)
        logger.info("registered model version", extra={"version": version.version})


def prune_realtime() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        deleted = ETLService().prune_realtime_states(
            db, settings.realtime_retention_days, settings.raw_snapshot_retention_days
        )
        logger.info("pruned realtime states", extra={"deleted": deleted})


def maintenance_cron(settings: Settings, *, offset_minutes: int = 0) -> CronTrigger:
    """Daily trigger in the service timezone at the off-peak maintenance time."""
    at = datetime.combine(date.today(), settings.maintenance_time) + timedelta(minutes=offset_minutes)
    return CronTrigger(hour=at.hour, minute=at.minute, timezone=settings.gtfs_service_tzinfo)


def register_jobs(scheduler: BaseScheduler, settings: Settings, *, include_training: bool = True) -> None:
    scheduler.add_job(
        poll_realtime_active_hours,
        "interval",
        seconds=settings.gtfs_poll_interval_seconds,
        max_instances=1,
    )
    scheduler.add_job(
        poll_realtime_off_hours,
        "interval",
        seconds=settings.gtfs_off_hours_poll_interval_seconds,
        max_instances=1,
    )
    # Heavy jobs run in the quiet hours after the service window ends, 30 min apart.
    scheduler.add_job(
        refresh_static, maintenance_cron(settings), max_instances=1, coalesce=True, misfire_grace_time=3600
    )
    scheduler.add_job(
        prune_realtime,
        maintenance_cron(settings, offset_minutes=30),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(refresh_features, "interval", minutes=settings.feature_refresh_minutes, max_instances=1)
    # Training needs pandas/sklearn, which the API image lacks, and far more
    # memory than the embedded scheduler has; it runs in GitHub Actions instead.
    if include_training and settings.training_in_worker:
        scheduler.add_job(run_training, "interval", hours=settings.training_refresh_hours, max_instances=1)


def run_bootstrap_jobs() -> None:
    try:
        refresh_static(only_missing=True)
        poll_realtime_active_hours()
        poll_realtime_off_hours()
        refresh_features()
    except Exception:
        logger.exception("bootstrap job failed")


def start_background_scheduler() -> BackgroundScheduler:
    """Run the scheduler inside the API process (EMBEDDED_SCHEDULER=true)."""
    Base.metadata.create_all(bind=engine)
    apply_migrations(engine)
    settings = get_settings()

    scheduler = BackgroundScheduler(timezone="UTC")
    register_jobs(scheduler, settings, include_training=False)
    # One-shot, off the request path so startup (and the healthcheck) isn't blocked.
    scheduler.add_job(run_bootstrap_jobs, next_run_time=datetime.now(timezone.utc), id="bootstrap")
    scheduler.start()
    logger.info("embedded scheduler started")
    return scheduler


def main() -> None:
    configure_logging()
    Base.metadata.create_all(bind=engine)
    apply_migrations(engine)
    settings = get_settings()

    scheduler = BlockingScheduler(timezone="UTC")
    register_jobs(scheduler, settings)

    logger.info("running initial bootstrap jobs")
    run_bootstrap_jobs()

    logger.info("scheduler started")
    scheduler.start()


if __name__ == "__main__":
    while True:
        try:
            main()
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.exception("scheduler crashed; restarting")
            time.sleep(5)
