import os
import pickle
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from knightbus import predictor as predictor_module
from knightbus.db import Base
from knightbus.models import Stop, StopTime, Trip, TripUpdateState, VehicleState
from knightbus.predictor import ETAPredictor


def build_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_predictor_prefers_trip_update_arrival() -> None:
    session = build_session()
    stop = Stop(stop_id="1001", name="Example Stop")
    trip = Trip(trip_id="trip-1", route_id="route-1")
    session.add_all([stop, trip])
    session.flush()
    session.add(
        StopTime(
            trip_id_ref=trip.id,
            stop_id_ref=stop.id,
            stop_sequence=1,
            arrival_time="10:00:00",
            departure_time="10:00:00",
            arrival_seconds=36000,
            departure_seconds=36000,
        )
    )
    session.add(
        TripUpdateState(
            trip_id="trip-1",
            route_id="route-1",
            stop_id="1001",
            arrival_time_unix=int(datetime.now(timezone.utc).timestamp()) + 600,
            delay_seconds=120,
        )
    )
    session.commit()

    items = ETAPredictor().get_stop_etas(session, "1001")

    assert items
    assert items[0].prediction_source == "heuristic"
    assert items[0].trip_id == "trip-1"


def test_predictor_uses_vehicle_progress_without_trip_updates() -> None:
    session = build_session()
    stop = Stop(stop_id="1002", name="Example Stop 2")
    trip = Trip(trip_id="trip-2", route_id="route-2")
    session.add_all([stop, trip])
    session.flush()
    session.add_all(
        [
            StopTime(
                trip_id_ref=trip.id,
                stop_id_ref=stop.id,
                stop_sequence=5,
                arrival_time="10:10:00",
                departure_time="10:10:00",
                arrival_seconds=36600,
                departure_seconds=36600,
            ),
            StopTime(
                trip_id_ref=trip.id,
                stop_id_ref=stop.id,
                stop_sequence=6,
                arrival_time="10:15:00",
                departure_time="10:15:00",
                arrival_seconds=36900,
                departure_seconds=36900,
            ),
            VehicleState(
                vehicle_id="vehicle-2",
                trip_id="trip-2",
                route_id="route-2",
                stop_id="1002",
                current_stop_sequence=5,
                timestamp_unix=int(datetime.now(timezone.utc).timestamp()) + 120,
            ),
        ]
    )
    session.commit()

    items = ETAPredictor().get_stop_etas(session, "1002")

    assert items
    assert items[0].prediction_source == "heuristic"
    assert items[0].vehicle_id == "vehicle-2"


class StubModel:
    def __init__(self, value: float) -> None:
        self.value = value

    def predict(self, df):  # noqa: ANN001
        return [self.value]


def write_artifact(path, value: float, mtime: float) -> None:  # noqa: ANN001
    with open(path, "wb") as handle:
        pickle.dump({"model": StubModel(value), "feature_columns": ["route_id"], "categorical_columns": []}, handle)
    os.utime(path, (mtime, mtime))


def make_predictor(monkeypatch, path) -> ETAPredictor:  # noqa: ANN001
    monkeypatch.setenv("MODEL_ARTIFACT_PATH", str(path))
    predictor_module.get_settings.cache_clear()
    try:
        return ETAPredictor()
    finally:
        predictor_module.get_settings.cache_clear()


def test_model_reloads_when_artifact_changes(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    path = tmp_path / "eta.pkl"
    write_artifact(path, 1.0, mtime=1000)
    p = make_predictor(monkeypatch, path)
    assert p.current_model().model.value == 1.0
    write_artifact(path, 2.0, mtime=2000)
    assert p.current_model().model.value == 2.0


def test_corrupt_artifact_keeps_previous_model(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    path = tmp_path / "eta.pkl"
    write_artifact(path, 1.0, mtime=1000)
    p = make_predictor(monkeypatch, path)
    path.write_bytes(b"not a pickle")
    os.utime(path, (2000, 2000))
    assert p.current_model().model.value == 1.0


def test_missing_artifact_falls_back_to_baseline(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    path = tmp_path / "eta.pkl"
    write_artifact(path, 1.0, mtime=1000)
    p = make_predictor(monkeypatch, path)
    path.unlink()
    assert p.current_model() is None
