"""Arrival labels must come from the same physical bus and GTFS feed."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, attributes

from knightbus.models import Base, Stop, StopTime, Trip, VehicleState
from knightbus.training import TrainingService


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        # SQLite drops timezone information; PostgreSQL returns these as UTC-aware.
        @event.listens_for(session, "loaded_as_persistent")
        def restore_utc(session, instance):
            if isinstance(instance, VehicleState) and instance.observed_at.tzinfo is None:
                attributes.set_committed_value(
                    instance, "observed_at", instance.observed_at.replace(tzinfo=timezone.utc),
                )
        yield session
    engine.dispose()


def add_trip(db: Session, feed: str, route: str = "r", lat: float = 3.0) -> None:
    trip = Trip(feed_name=feed, trip_id="shared", route_id=route)
    stops = [Stop(feed_name=feed, stop_id=f"s{i}", name=f"Stop {i}", lat=lat+i*.01, lon=101.7)
             for i in range(2)]
    db.add_all([trip, *stops])
    db.flush()
    db.add_all([StopTime(trip_id_ref=trip.id, stop_id_ref=stop.id, stop_sequence=i+1,
                         arrival_seconds=43200+i*600) for i, stop in enumerate(stops)])


NOW = datetime(2026, 11, 23, 4, tzinfo=timezone.utc)


def position(db: Session, bus: str, seconds: int, *, feed: str | None = "kl",
             arrival: bool = False, lat: float = 3.0, source: str = "gtfs") -> None:
    observed = NOW + timedelta(seconds=seconds)
    db.add(VehicleState(feed_name=feed, trip_id="shared", route_id="r", vehicle_id=bus,
                        lat=lat+(.01 if arrival else .005), lon=101.7,
                        current_stop_sequence=2 if arrival else 1, observed_at=observed,
                        timestamp_unix=int(observed.timestamp()), position_source=source))


@pytest.mark.parametrize("slow_arrival", [90, 180])
def test_buses_sharing_trip_keep_their_own_future_arrivals(db, slow_arrival):
    add_trip(db, "kl")
    position(db, "slow", 0)
    position(db, "fast", 10)
    position(db, "fast", 60, arrival=True)
    position(db, "slow", slow_arrival, arrival=True)
    db.commit()

    rows = TrainingService()._collect_training_rows(db)
    labels = {row.observed_at: row.label_eta_seconds for row in rows}
    assert labels == {NOW: slow_arrival, NOW + timedelta(seconds=10): 50}
    assert all(row.features["stop_id"] == "s1" for row in rows)


def test_bus_without_own_arrival_cannot_borrow_another_buses_label(db):
    add_trip(db, "kl")
    position(db, "missing", 0)
    position(db, "tracked", 10)
    position(db, "tracked", 60, arrival=True)
    db.commit()

    rows = TrainingService()._collect_training_rows(db)
    assert [(row.observed_at, row.label_eta_seconds) for row in rows] == [
        (NOW + timedelta(seconds=10), 50),
    ]


def test_identical_trip_and_vehicle_ids_in_different_feeds_do_not_mix(db):
    add_trip(db, "kl", "kl-route")
    add_trip(db, "penang", "penang-route", lat=5.0)
    position(db, "same-bus-id", 0)
    position(db, "same-bus-id", 180, arrival=True)
    position(db, "same-bus-id", 10, feed="penang", lat=5.0)
    position(db, "same-bus-id", 60, feed="penang", lat=5.0, arrival=True)
    db.commit()

    rows = TrainingService()._collect_training_rows(db)
    assert {(row.features["route_id"], row.observed_at, row.label_eta_seconds) for row in rows} == {
        ("kl-route", NOW, 180),
        ("penang-route", NOW + timedelta(seconds=10), 50),
    }
    assert len(rows) == 2


def test_unknown_feed_and_kiosk_positions_cannot_supply_training_arrivals(db):
    add_trip(db, "kl")
    position(db, "bus", 0)
    position(db, "bus", 60, feed=None, arrival=True)
    position(db, "bus", 90, source="kiosk", arrival=True)
    position(db, "bus", 180, arrival=True)
    db.commit()

    rows = TrainingService()._collect_training_rows(db)
    assert [(row.observed_at, row.label_eta_seconds) for row in rows] == [(NOW, 180)]
