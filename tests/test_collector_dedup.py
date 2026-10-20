from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from knightbus.collector import GTFSCollector
from knightbus.db import Base
from knightbus.models import VehicleState

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_latest_vehicle_timestamps() -> None:
    db = build_session()
    db.add_all([
        VehicleState(feed_name="kl", vehicle_id="v1", timestamp_unix=100, observed_at=NOW),
        VehicleState(feed_name="kl", vehicle_id="v1", timestamp_unix=130, observed_at=NOW),
        VehicleState(feed_name="kl", vehicle_id="v2", timestamp_unix=None, observed_at=NOW),
        VehicleState(feed_name="penang", vehicle_id="v1", timestamp_unix=999, observed_at=NOW),
        VehicleState(feed_name="kl", vehicle_id="v3", timestamp_unix=50, observed_at=NOW - timedelta(hours=2)),
    ])
    db.commit()

    result = GTFSCollector.latest_vehicle_timestamps(db, "kl", since=NOW - timedelta(hours=1))

    assert result == {"v1": 130}
