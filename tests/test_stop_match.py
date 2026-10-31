from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from knightbus.collector import GTFSCollector
from knightbus.db import Base
from knightbus.models import Stop, StopTime, Trip, VehicleState


def build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def seed(db):
    s1 = Stop(feed_name="mrt", stop_id="A", lat=3.0000, lon=101.0000)
    s2 = Stop(feed_name="mrt", stop_id="B", lat=3.0100, lon=101.0000)
    trip = Trip(feed_name="mrt", trip_id="T1", route_id="static-route")
    db.add_all([s1, s2, trip])
    db.flush()
    db.add_all([
        StopTime(trip_id_ref=trip.id, stop_id_ref=s1.id, stop_sequence=1),
        StopTime(trip_id_ref=trip.id, stop_id_ref=s2.id, stop_sequence=2),
    ])
    db.commit()


def test_matches_by_trip_when_realtime_route_id_is_unknown() -> None:
    db = build_session()
    seed(db)

    matched = GTFSCollector._match_nearest_stop(
        db, 3.0101, 101.0, "realtime-only-route", {}, feed_name="mrt", trip_id="T1"
    )

    assert matched == ("B", 2)


def test_falls_back_to_route_without_trip() -> None:
    db = build_session()
    seed(db)

    matched = GTFSCollector._match_nearest_stop(db, 3.0001, 101.0, "static-route", {})

    assert matched == ("A", 1)


def test_no_match_beyond_radius() -> None:
    db = build_session()
    seed(db)

    assert GTFSCollector._match_nearest_stop(
        db, 3.005, 101.0, None, {}, feed_name="mrt", trip_id="T1"
    ) is None


def vehicle_payload(lat: float, lon: float, timestamp: int):
    from google.transit import gtfs_realtime_pb2 as gtfs_rt

    from knightbus.collector import FeedPayload

    msg = gtfs_rt.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    entity = msg.entity.add()
    entity.id = "e1"
    entity.vehicle.vehicle.id = "bus-1"
    entity.vehicle.trip.trip_id = "T1"
    entity.vehicle.position.latitude = lat
    entity.vehicle.position.longitude = lon
    entity.vehicle.timestamp = timestamp
    return FeedPayload(url="x", fetched_at=datetime(2026, 10, 12, tzinfo=timezone.utc), payload=msg.SerializeToString())


def test_vehicle_poll_bulk_inserts_and_reuses_stop_cache(monkeypatch) -> None:
    from knightbus import collector as collector_module
    from knightbus.config import FeedConfig

    db = build_session()
    seed(db)
    collector_module._STOP_CANDIDATES.clear()
    collector = GTFSCollector(FeedConfig(name="mrt", static_url="x"))

    calls = []
    original = GTFSCollector._stops_where

    def counting(db_, *conditions):
        calls.append(1)
        return original(db_, *conditions)

    monkeypatch.setattr(GTFSCollector, "_stops_where", staticmethod(counting))

    assert collector.normalize_vehicle_positions(db, vehicle_payload(3.0101, 101.0, 100)) == 1
    assert collector.normalize_vehicle_positions(db, vehicle_payload(3.0001, 101.0, 130)) == 1

    rows = db.execute(select(VehicleState.stop_id, VehicleState.current_stop_sequence).order_by(VehicleState.id)).all()
    assert rows == [("B", 2), ("A", 1)]
    assert len(calls) == 1  # second poll hit the cross-poll cache
    collector_module._STOP_CANDIDATES.clear()
