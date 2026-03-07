import logging
import time

from apscheduler.schedulers.blocking import BlockingScheduler

from knightbus.collector import GTFSCollector
from knightbus.config import get_settings
from knightbus.db import Base, SessionLocal, engine
from knightbus.etl import ETLService
from knightbus.logging import configure_logging
from knightbus.training import TrainingService

logger = logging.getLogger(__name__)


def poll_realtime() -> None:
    collector = GTFSCollector()
    with SessionLocal() as db:
        vehicle_payload = collector.fetch_vehicle_positions()
        if vehicle_payload:
            inserted = collector.normalize_vehicle_positions(db, vehicle_payload)
            logger.info("stored vehicle positions", extra={"inserted": inserted})

        trip_payload = collector.fetch_trip_updates()
        if trip_payload:
            inserted = collector.normalize_trip_updates(db, trip_payload)
            logger.info("stored trip updates", extra={"inserted": inserted})


def refresh_static() -> None:
    collector = GTFSCollector()
    with SessionLocal() as db:
        payload = collector.fetch_static()
        if payload:
            collector.load_static_feed(db, payload)
            logger.info("refreshed GTFS static feed")


def refresh_features() -> None:
    with SessionLocal() as db:
        count = ETLService().refresh_segment_statistics(db)
        logger.info("refreshed segment statistics", extra={"updated": count})


def run_training() -> None:
    with SessionLocal() as db:
        version = TrainingService().run_training(db)
        logger.info("registered model version", extra={"version": version.version})


def main() -> None:
    configure_logging()
    Base.metadata.create_all(bind=engine)
    settings = get_settings()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(poll_realtime, "interval", seconds=settings.gtfs_poll_interval_seconds, max_instances=1)
    scheduler.add_job(refresh_static, "interval", hours=settings.gtfs_static_refresh_hours, max_instances=1)
    scheduler.add_job(refresh_features, "interval", minutes=settings.feature_refresh_minutes, max_instances=1)
    scheduler.add_job(run_training, "interval", hours=settings.training_refresh_hours, max_instances=1)

    logger.info("running initial bootstrap jobs")
    try:
        refresh_static()
        poll_realtime()
        refresh_features()
    except Exception:
        logger.exception("bootstrap job failed")

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

