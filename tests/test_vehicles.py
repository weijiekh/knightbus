from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from knightbus.models import Base, Stop, Trip, VehicleState
from knightbus.vehicles import route_vehicles


def test_route_metadata_stays_with_its_feed_and_missing_static_is_preserved():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([
            Trip(feed_name="a", trip_id="t", headsign="North", direction_id=0),
            Trip(feed_name="b", trip_id="t", headsign="South", direction_id=1),
            Stop(feed_name="a", stop_id="s", name="North stop"),
            Stop(feed_name="b", stop_id="s", name="South stop"),
            VehicleState(feed_name="a", vehicle_id="one", route_id="r", trip_id="t", stop_id="s", observed_at=datetime.now(timezone.utc)),
            VehicleState(feed_name="missing", vehicle_id="two", route_id="r", trip_id="t", stop_id="s"),
            VehicleState(feed_name="a", vehicle_id="other", route_id="other"),
        ])
        db.commit()
        buses = {v.vehicle_id: v for v in route_vehicles(db, "r")}
        assert set(buses) == {"one", "two"}
        assert buses["one"].headsign == "North"
        assert buses["one"].direction_id == 0
        assert buses["one"].stop_name == "North stop"
        assert buses["two"].headsign is None
        assert buses["two"].stop_name is None
        assert route_vehicles(db, "unknown") == []


def test_route_returns_only_latest_recent_position_and_excludes_switched_buses():
    from datetime import timedelta

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        db.add_all([
            VehicleState(feed_name="a", vehicle_id="bus", route_id="r", lat=1, observed_at=now - timedelta(minutes=2)),
            VehicleState(feed_name="a", vehicle_id="bus", route_id="r", lat=2, observed_at=now),
            VehicleState(feed_name="a", vehicle_id="switched", route_id="r", observed_at=now - timedelta(minutes=1)),
            VehicleState(feed_name="a", vehicle_id="switched", route_id="other", observed_at=now),
            VehicleState(feed_name="a", vehicle_id="old", route_id="r", observed_at=now - timedelta(hours=1)),
            VehicleState(feed_name="b", vehicle_id="bus", route_id="r", lat=3, observed_at=now),
        ])
        db.commit()
        buses = route_vehicles(db, "r")
        assert len(buses) == 2
        assert sorted(v.lat for v in buses) == [2, 3]
