from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from knightbus.db import Base
from knightbus.etl import ETLService
from knightbus.models import RawSnapshot, TripUpdateState, VehicleState

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_prune_removes_only_old_rows() -> None:
    db = build_session()
    db.add_all([
        VehicleState(vehicle_id="old", observed_at=NOW - timedelta(days=15)),
        VehicleState(vehicle_id="new", observed_at=NOW - timedelta(days=13)),
        TripUpdateState(trip_id="old-trip", observed_at=NOW - timedelta(days=15)),
        TripUpdateState(trip_id="new-trip", observed_at=NOW - timedelta(days=13)),
        RawSnapshot(feed_type="vehicle_positions", source_url="x", payload=b"a", fetched_at=NOW - timedelta(days=3)),
        RawSnapshot(feed_type="vehicle_positions", source_url="x", payload=b"b", fetched_at=NOW - timedelta(hours=1)),
    ])
    db.commit()

    deleted = ETLService().prune_realtime_states(db, realtime_days=14, raw_days=2, now=NOW)

    assert deleted == 3
    assert db.execute(select(VehicleState.vehicle_id)).scalars().all() == ["new"]
    assert db.execute(select(TripUpdateState.trip_id)).scalars().all() == ["new-trip"]
    assert db.execute(select(RawSnapshot.payload)).scalars().all() == [b"b"]


def test_prune_disabled_when_days_not_positive() -> None:
    db = build_session()
    db.add(VehicleState(vehicle_id="old", observed_at=NOW - timedelta(days=400)))
    db.commit()

    assert ETLService().prune_realtime_states(db, realtime_days=0, raw_days=0, now=NOW) == 0
    assert db.execute(select(func.count()).select_from(VehicleState)).scalar_one() == 1
