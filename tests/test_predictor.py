import os
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


def touch_artifact(path, mtime: float) -> None:  # noqa: ANN001
    path.write_bytes(b"artifact")
    os.utime(path, (mtime, mtime))


def patch_loader(monkeypatch, results: list) -> None:  # noqa: ANN001
    """Make load_residual_model return successive entries of `results`."""
    queue = list(results)
    monkeypatch.setattr(predictor_module, "load_residual_model", lambda _path: queue.pop(0))


def make_predictor(monkeypatch, path) -> ETAPredictor:  # noqa: ANN001
    monkeypatch.setenv("MODEL_ARTIFACT_PATH", str(path))
    predictor_module.get_settings.cache_clear()
    try:
        return ETAPredictor()
    finally:
        predictor_module.get_settings.cache_clear()


def test_model_reloads_when_artifact_changes(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    path = tmp_path / "eta.pkl"
    touch_artifact(path, 1000)
    first, second = StubModel(1.0), StubModel(2.0)
    patch_loader(monkeypatch, [first, second])
    p = make_predictor(monkeypatch, path)
    assert p.current_model() is first
    touch_artifact(path, 2000)
    assert p.current_model() is second


def test_corrupt_artifact_keeps_previous_model(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    path = tmp_path / "eta.pkl"
    touch_artifact(path, 1000)
    first = StubModel(1.0)
    patch_loader(monkeypatch, [first, None])
    p = make_predictor(monkeypatch, path)
    touch_artifact(path, 2000)
    assert p.current_model() is first


def test_missing_artifact_falls_back_to_baseline(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    path = tmp_path / "eta.pkl"
    touch_artifact(path, 1000)
    patch_loader(monkeypatch, [StubModel(1.0)])
    p = make_predictor(monkeypatch, path)
    path.unlink()
    assert p.current_model() is None
