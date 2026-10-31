from contextlib import nullcontext
from datetime import datetime

import pytest

from knightbus.config import Settings
from knightbus import scheduler


def build_settings(**overrides: object) -> Settings:
    values = {
        "GTFS_SERVICE_TIMEZONE": "Asia/Kuala_Lumpur",
        "GTFS_ACTIVE_POLL_START_LOCAL": "06:00",
        "GTFS_ACTIVE_POLL_END_LOCAL": "00:00",
        "GTFS_POLL_INTERVAL_SECONDS": 30,
        "GTFS_OFF_HOURS_POLL_INTERVAL_SECONDS": 900,
        "GTFS_FEEDS": [],
    }
    values.update(overrides)
    return Settings(**values)


def test_should_run_realtime_poll_during_active_hours() -> None:
    settings = build_settings()

    assert scheduler.should_run_realtime_poll(
        active_hours=True,
        now=datetime(2026, 3, 8, 7, 0, tzinfo=settings.gtfs_service_tzinfo),
        settings=settings,
    )
    assert not scheduler.should_run_realtime_poll(
        active_hours=False,
        now=datetime(2026, 3, 8, 7, 0, tzinfo=settings.gtfs_service_tzinfo),
        settings=settings,
    )


def test_should_run_realtime_poll_during_off_hours() -> None:
    settings = build_settings()

    assert scheduler.should_run_realtime_poll(
        active_hours=False,
        now=datetime(2026, 3, 8, 2, 0, tzinfo=settings.gtfs_service_tzinfo),
        settings=settings,
    )
    assert not scheduler.should_run_realtime_poll(
        active_hours=True,
        now=datetime(2026, 3, 8, 2, 0, tzinfo=settings.gtfs_service_tzinfo),
        settings=settings,
    )


def test_window_boundaries_are_start_inclusive_and_end_exclusive() -> None:
    settings = build_settings()

    assert scheduler.should_run_realtime_poll(
        active_hours=True,
        now=datetime(2026, 3, 8, 6, 0, tzinfo=settings.gtfs_service_tzinfo),
        settings=settings,
    )
    assert not scheduler.should_run_realtime_poll(
        active_hours=True,
        now=datetime(2026, 3, 9, 0, 0, tzinfo=settings.gtfs_service_tzinfo),
        settings=settings,
    )


def test_cross_midnight_windows_are_supported() -> None:
    settings = build_settings(
        GTFS_ACTIVE_POLL_START_LOCAL="22:00",
        GTFS_ACTIVE_POLL_END_LOCAL="04:00",
    )

    assert scheduler.should_run_realtime_poll(
        active_hours=True,
        now=datetime(2026, 3, 8, 23, 30, tzinfo=settings.gtfs_service_tzinfo),
        settings=settings,
    )
    assert scheduler.should_run_realtime_poll(
        active_hours=True,
        now=datetime(2026, 3, 9, 1, 30, tzinfo=settings.gtfs_service_tzinfo),
        settings=settings,
    )
    assert not scheduler.should_run_realtime_poll(
        active_hours=True,
        now=datetime(2026, 3, 8, 12, 0, tzinfo=settings.gtfs_service_tzinfo),
        settings=settings,
    )


def test_invalid_local_time_configuration_fails_fast() -> None:
    with pytest.raises(ValueError, match="invalid local time"):
        build_settings(GTFS_ACTIVE_POLL_START_LOCAL="6am")


def test_invalid_off_hours_interval_fails_fast() -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        build_settings(GTFS_OFF_HOURS_POLL_INTERVAL_SECONDS=0)


def test_active_hours_poll_skips_without_opening_db(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build_settings()
    opened_db = False

    def fake_session_local():
        nonlocal opened_db
        opened_db = True
        return nullcontext()

    monkeypatch.setattr(scheduler, "get_settings", lambda: settings)
    monkeypatch.setattr(scheduler, "SessionLocal", fake_session_local)
    # Pin "outside the active window" so the result doesn't depend on the wall clock.
    monkeypatch.setattr(
        scheduler,
        "should_run_realtime_poll",
        lambda **kwargs: kwargs["active_hours"] is False,
    )

    scheduler.poll_realtime(
        active_hours=True,
        poll_label="active_hours",
    )

    assert not opened_db


def test_off_hours_poll_executes_realtime_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build_settings()
    calls: list[str] = []

    class FakeCollector:
        def __init__(self, feed=None):
            pass

        def fetch_vehicle_positions(self):
            calls.append("fetch_vehicle_positions")
            return None

        def fetch_trip_updates(self):
            calls.append("fetch_trip_updates")
            return None

    monkeypatch.setattr(scheduler, "get_settings", lambda: settings)
    monkeypatch.setattr(
        scheduler,
        "should_run_realtime_poll",
        lambda **kwargs: kwargs["active_hours"] is False,
    )
    monkeypatch.setattr(scheduler, "GTFSCollector", FakeCollector)
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: nullcontext(object()))

    scheduler.poll_realtime(
        active_hours=False,
        poll_label="off_hours",
    )

    assert calls == ["fetch_vehicle_positions", "fetch_trip_updates"]


from apscheduler.schedulers.background import BackgroundScheduler


def job_names(settings: Settings) -> set[str]:
    sched = BackgroundScheduler(timezone="UTC")
    scheduler.register_jobs(sched, settings)
    return {job.func.__name__ for job in sched.get_jobs()}


def test_training_job_registered_by_default() -> None:
    assert "run_training" in job_names(build_settings())


def test_training_job_skipped_when_disabled() -> None:
    assert "run_training" not in job_names(build_settings(TRAINING_IN_WORKER=False))


def test_prune_job_registered() -> None:
    assert "prune_realtime" in job_names(build_settings())


def test_heavy_jobs_are_scheduled_off_peak_and_training_is_excluded_when_embedded() -> None:
    from apscheduler.schedulers.background import BackgroundScheduler

    settings = build_settings(MAINTENANCE_LOCAL_TIME="04:00", TRAINING_IN_WORKER=True)
    sched = BackgroundScheduler(timezone="UTC")

    scheduler.register_jobs(sched, settings, include_training=False)

    jobs = {job.func.__name__: job for job in sched.get_jobs()}
    assert "run_training" not in jobs
    assert str(jobs["refresh_static"].trigger).startswith("cron[hour='4', minute='0']")
    assert str(jobs["prune_realtime"].trigger).startswith("cron[hour='4', minute='30']")


def test_training_job_registered_for_standalone_worker() -> None:
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(timezone="UTC")

    scheduler.register_jobs(sched, build_settings(TRAINING_IN_WORKER=True))

    assert "run_training" in {job.func.__name__ for job in sched.get_jobs()}


def test_refresh_static_only_missing_skips_loaded_feeds(monkeypatch) -> None:
    from datetime import timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from knightbus.config import FeedConfig
    from knightbus.db import Base
    from knightbus.models import StaticFeedState

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(StaticFeedState(feed_name="kl", sha256="x", loaded_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        db.commit()

    fetched: list[str] = []

    class FakeCollector:
        def __init__(self, feed: FeedConfig) -> None:
            self.feed = feed

        def fetch_static(self):
            fetched.append(self.feed.name)
            return None

    settings = build_settings(GTFS_FEEDS=[FeedConfig(name="kl", static_url="x"), FeedConfig(name="penang", static_url="x")])
    monkeypatch.setattr(scheduler, "SessionLocal", Session)
    monkeypatch.setattr(scheduler, "GTFSCollector", FakeCollector)
    monkeypatch.setattr(scheduler, "get_settings", lambda: settings)

    scheduler.refresh_static(only_missing=True)

    assert fetched == ["penang"]
