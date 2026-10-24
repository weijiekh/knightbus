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


def test_stat_oserror_keeps_previous_model(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    path = tmp_path / "eta.pkl"
    touch_artifact(path, 1000)
    first = StubModel(1.0)
    patch_loader(monkeypatch, [first])
    p = make_predictor(monkeypatch, path)

    def boom(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise PermissionError("denied")

    monkeypatch.setattr(type(path), "stat", boom)
    assert p.current_model() is first


def test_stop_etas_endpoint_includes_stop_name_and_code(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool

    from knightbus import main as main_module
    from knightbus.db import get_db
    from knightbus.schemas import EtaItem

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    session.add_all(
        [
            Stop(stop_id="1001", name="KL105 LRT MASJID JAMEK", lat=3.149, lon=101.696),
            Stop(stop_id="1002", name="Plain Stop"),
        ]
    )
    session.commit()
    now = datetime.now(timezone.utc)
    item = EtaItem(
        route_id="r", trip_id="t", stop_id="x", prediction_source="heuristic", data_timestamp=now
    )
    monkeypatch.setattr(main_module.predictor, "get_stop_etas", lambda *a, **k: [item])
    main_module.app.dependency_overrides[get_db] = lambda: session
    try:
        client = TestClient(main_module.app)
        body = client.get("/v1/stops/1001/etas").json()
        assert body["stop_code"] == "KL105"
        assert body["stop_name"] == "LRT MASJID JAMEK"
        assert (body["lat"], body["lon"]) == (3.149, 101.696)
        plain = client.get("/v1/stops/1002/etas").json()
        assert plain["stop_code"] is None and plain["stop_name"] == "Plain Stop"
        unknown = client.get("/v1/stops/9999/etas").json()
        assert unknown["stop_name"] is None and unknown["stop_code"] is None
    finally:
        main_module.app.dependency_overrides.clear()


def _local_seconds() -> int:
    from zoneinfo import ZoneInfo

    local = datetime.now(ZoneInfo("Asia/Kuala_Lumpur"))
    return local.hour * 3600 + local.minute * 60 + local.second


def _stop_with_trip(session, trip_id: str, sequences: dict[int, int], stop: Stop | None = None):  # noqa: ANN001
    stop = stop or Stop(stop_id=f"s-{trip_id}", name="S")
    trip = Trip(trip_id=trip_id, route_id="r")
    session.add_all([stop, trip])
    session.flush()
    for seq, secs in sequences.items():
        session.add(StopTime(trip_id_ref=trip.id, stop_id_ref=stop.id, stop_sequence=seq, arrival_seconds=secs))
    session.commit()
    return stop, trip


def test_stop_etas_return_upcoming_trips_not_the_days_earliest() -> None:
    session = build_session()
    now_s = _local_seconds()
    stop = Stop(stop_id="up", name="S")
    session.add(stop)
    session.flush()
    for i, secs in enumerate([max(0, now_s - 7200), now_s + 600]):
        trip = Trip(trip_id=f"t{i}", route_id="r")
        session.add(trip)
        session.flush()
        session.add(StopTime(trip_id_ref=trip.id, stop_id_ref=stop.id, stop_sequence=3, arrival_seconds=secs))
    session.commit()

    items = ETAPredictor().get_stop_etas(session, "up")

    assert [i.trip_id for i in items] == ["t1"]


def test_vehicle_elsewhere_on_the_trip_sets_delay_and_passed_bus_is_dropped() -> None:
    session = build_session()
    now_s = _local_seconds()
    stop, trip = _stop_with_trip(session, "live", {2: now_s + 300, 5: now_s + 900})
    stop2, _ = _stop_with_trip(session, "gone", {7: now_s + 300})
    session.add_all([
        VehicleState(vehicle_id="v-live", trip_id="live", stop_id="elsewhere", current_stop_sequence=2,
                     observed_at=datetime.now(timezone.utc), timestamp_unix=int(datetime.now(timezone.utc).timestamp())),
        VehicleState(vehicle_id="v-gone", trip_id="gone", stop_id="elsewhere", current_stop_sequence=9,
                     observed_at=datetime.now(timezone.utc)),
    ])
    session.commit()

    live = ETAPredictor().get_stop_etas(session, stop.stop_id)
    gone = ETAPredictor().get_stop_etas(session, stop2.stop_id)

    assert live and live[0].vehicle_id == "v-live" and live[0].prediction_source == "heuristic"
    assert gone == []
