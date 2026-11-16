"""The same bus/stop/evidence must have the same ETA in both API views."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from knightbus import predictor as predictor_module, trips
from knightbus.db import Base
from knightbus.models import Route, Stop, StopTime, Trip, TripUpdateState, VehicleState
from knightbus.predictor import ETAPredictor


@pytest.fixture
def scenario(monkeypatch):
    clock = {"now": datetime(2026, 11, 17, 4, tzinfo=timezone.utc)}  # noon MYT
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz)
    monkeypatch.setattr(predictor_module, "datetime", Clock)
    monkeypatch.setattr(trips, "datetime", Clock)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        trip = Trip(trip_id="t", route_id="r")
        stops = [Stop(stop_id=f"s{i}", name=f"Stop {i}", lat=3.02+i/100, lon=101.71) for i in range(3)]
        db.add_all([Route(route_id="r", short_name="190"), trip, *stops])
        db.flush()
        for i, seconds in enumerate((42600, 43500, 43800)):
            db.add(StopTime(trip_id_ref=trip.id, stop_id_ref=stops[i].id, stop_sequence=i+1, arrival_seconds=seconds))
        observed = clock["now"] - timedelta(seconds=30)
        db.add(VehicleState(feed_name="default", vehicle_id="bus", trip_id="t", route_id="r",
                            lat=3.02, lon=101.71, current_stop_sequence=1, observed_at=observed,
                            timestamp_unix=int(observed.timestamp())))
        db.commit()
        predictor = ETAPredictor()
        monkeypatch.setattr(trips, "get_predictor", lambda: predictor)
        yield db, predictor, clock


class OffsetModel:
    def predict(self, features):
        return -300.0


def test_model_adjusted_arrival_is_identical_and_stable_between_page_requests(scenario, monkeypatch):
    db, predictor, clock = scenario
    monkeypatch.setattr(predictor, "current_model", lambda: OffsetModel())
    stop = predictor.get_stop_etas(db, "s1")[0]
    assert stop.prediction_source == "ml_residual"
    # The previous journey implementation would have shown 14m30s here,
    # while the stop card included a -5 minute model adjustment.
    assert stop.predicted_arrival_time == clock["now"] + timedelta(seconds=570)
    clock["now"] += timedelta(seconds=15)
    bus = trips.get_trip_journey(db, "t").buses[0]
    assert bus.arrivals[2] == stop.predicted_arrival_time
    assert bus.arrival_sources[2] == stop.prediction_source
    again = predictor.get_stop_etas(db, "s1")[0]
    assert again.predicted_arrival_time == stop.predicted_arrival_time
    assert again.vehicle_id == bus.vehicle_id and again.stop_sequence == 2


def test_explicit_provider_arrival_is_shared_and_overrides_model(scenario, monkeypatch):
    db, predictor, clock = scenario
    monkeypatch.setattr(predictor, "current_model", lambda: OffsetModel())
    arrival = clock["now"] + timedelta(seconds=123)
    db.add(TripUpdateState(feed_name="default", trip_id="t", stop_id="s1", stop_sequence=2,
                           arrival_time_unix=int(arrival.timestamp()), observed_at=clock["now"]))
    db.commit()
    stop = predictor.get_stop_etas(db, "s1")[0]
    bus = trips.get_trip_journey(db, "t").buses[0]
    assert stop.predicted_arrival_time == bus.arrivals[2] == arrival
    assert bus.arrival_sources[2] == "heuristic"
    assert stop.minutes_away == 3


def test_invalid_intermediate_schedule_suppresses_both_views(scenario, monkeypatch):
    from sqlalchemy import update
    db, predictor, clock = scenario
    monkeypatch.setattr(predictor, "current_model", lambda: None)
    db.execute(update(StopTime).where(StopTime.stop_sequence == 2).values(arrival_seconds=None))
    db.commit()
    assert predictor.get_stop_etas(db, "s2") == []
    assert trips.get_trip_journey(db, "t").buses[0].arrivals == {}
