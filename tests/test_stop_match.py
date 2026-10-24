from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from knightbus.collector import GTFSCollector
from knightbus.db import Base
from knightbus.models import Stop, StopTime, Trip


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
