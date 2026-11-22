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


def test_each_bus_sharing_a_trip_keeps_its_own_eta_and_passed_stop_filter(scenario, monkeypatch):
    db, predictor, clock = scenario
    monkeypatch.setattr(predictor, "current_model", lambda: None)
    observed = clock["now"] - timedelta(seconds=10)
    db.add(VehicleState(feed_name="default", vehicle_id="ahead", trip_id="t", route_id="r",
                        lat=3.03, lon=101.71, current_stop_sequence=2, observed_at=observed,
                        timestamp_unix=int(observed.timestamp())))
    # A historic record for the same bus must not create another arrival.
    db.add(VehicleState(feed_name="default", vehicle_id="ahead", trip_id="t", route_id="r",
                        lat=3.02, lon=101.71, current_stop_sequence=1,
                        observed_at=observed - timedelta(seconds=20),
                        timestamp_unix=int(observed.timestamp()) - 20))
    db.commit()
    buses = {b.vehicle_id: b for b in trips.get_trip_journey(db, "t").buses}
    arrivals = {item.vehicle_id: item for item in predictor.get_stop_etas(db, "s2")}
    assert set(arrivals) == {"bus", "ahead"}
    for vehicle_id, item in arrivals.items():
        assert item.predicted_arrival_time == buses[vehicle_id].arrivals[3]
    assert arrivals["ahead"].predicted_arrival_time < arrivals["bus"].predicted_arrival_time
    # Move the leading bus beyond the stop: the trailing bus must remain visible.
    db.add(VehicleState(feed_name="default", vehicle_id="ahead", trip_id="t", route_id="r",
                        lat=3.04, lon=101.71, current_stop_sequence=3, observed_at=clock["now"],
                        timestamp_unix=int(clock["now"].timestamp())))
    db.commit()
    assert [item.vehicle_id for item in predictor.get_stop_etas(db, "s1")] == ["bus"]


def test_model_residuals_cannot_reverse_stop_order_in_either_view(scenario, monkeypatch):
    db, predictor, clock = scenario

    class ReversingModel:
        def predict(self, features):
            return 0 if features["stop_sequence"] == 2 else -900

    monkeypatch.setattr(predictor, "current_model", lambda: ReversingModel())
    bus = trips.get_trip_journey(db, "t").buses[0]
    assert bus.arrivals[3] >= bus.arrivals[2]
    for stop_id, sequence in (("s1", 2), ("s2", 3)):
        item = predictor.get_stop_etas(db, stop_id)[0]
        assert item.predicted_arrival_time == bus.arrivals[sequence]
        expected = (item.predicted_arrival_time - clock["now"]).total_seconds()
        assert item.minutes_away == int((expected + 59) // 60)
    clock["now"] += timedelta(seconds=15)
    again = trips.get_trip_journey(db, "t").buses[0]
    assert again.arrivals == bus.arrivals


def test_vehicle_specific_update_is_not_applied_to_another_bus(scenario, monkeypatch):
    db, predictor, clock = scenario
    monkeypatch.setattr(predictor, "current_model", lambda: None)
    observed = clock["now"] - timedelta(seconds=10)
    db.add(VehicleState(feed_name="default", vehicle_id="ahead", trip_id="t", route_id="r",
                        lat=3.03, lon=101.71, current_stop_sequence=2, observed_at=observed,
                        timestamp_unix=int(observed.timestamp())))
    arrival = clock["now"] + timedelta(seconds=123)
    db.add(TripUpdateState(feed_name="default", trip_id="t", stop_id="s2", stop_sequence=3,
                           vehicle_id="ahead", arrival_time_unix=int(arrival.timestamp()),
                           observed_at=clock["now"]))
    db.commit()
    buses = {b.vehicle_id: b for b in trips.get_trip_journey(db, "t").buses}
    arrivals = {item.vehicle_id: item.predicted_arrival_time for item in predictor.get_stop_etas(db, "s2")}
    assert arrivals["ahead"] == buses["ahead"].arrivals[3] == arrival
    assert arrivals["bus"] == buses["bus"].arrivals[3] > arrival
