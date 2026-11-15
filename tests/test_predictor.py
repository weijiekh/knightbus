import os
from datetime import datetime, timedelta, timezone

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
            arrival_seconds=_local_seconds() + 600,
            departure_seconds=_local_seconds() + 600,
        )
    )
    session.add(StopTime(trip_id_ref=trip.id, stop_id_ref=stop.id,
                         stop_sequence=0, arrival_seconds=max(0, _local_seconds() - 600)))
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
                arrival_seconds=_local_seconds() - 120,
                departure_seconds=_local_seconds() - 120,
            ),
            StopTime(
                trip_id_ref=trip.id,
                stop_id_ref=stop.id,
                stop_sequence=6,
                arrival_time="10:15:00",
                departure_time="10:15:00",
                arrival_seconds=_local_seconds() + 300,
                departure_seconds=_local_seconds() + 300,
            ),
            VehicleState(lat=3.02, lon=101.71,
                vehicle_id="vehicle-2",
                trip_id="trip-2",
                route_id="route-2",
                stop_id="1002",
                current_stop_sequence=5,
                timestamp_unix=int(datetime.now(timezone.utc).timestamp()),
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
    stop, trip = _stop_with_trip(session, "live", {1: max(0, now_s - 600), 2: now_s + 300, 5: now_s + 900})
    stop2, _ = _stop_with_trip(session, "gone", {7: now_s + 300})
    session.add_all([
        VehicleState(lat=3.02, lon=101.71, vehicle_id="v-live", trip_id="live", stop_id="elsewhere", current_stop_sequence=2,
                     observed_at=datetime.now(timezone.utc), timestamp_unix=int(datetime.now(timezone.utc).timestamp())),
        VehicleState(lat=3.02, lon=101.71, vehicle_id="v-gone", trip_id="gone", stop_id="elsewhere", current_stop_sequence=9,
                     observed_at=datetime.now(timezone.utc)),
    ])
    session.commit()

    live = ETAPredictor().get_stop_etas(session, stop.stop_id)
    gone = ETAPredictor().get_stop_etas(session, stop2.stop_id)

    assert live and live[0].vehicle_id == "v-live" and live[0].prediction_source == "heuristic"
    assert gone == []


def test_nearby_stops_filters_by_radius_and_sorts_by_distance() -> None:
    session = build_session()
    session.add_all([
        Stop(stop_id="near", name="Near", lat=3.1400, lon=101.6900),
        Stop(stop_id="mid", name="Mid", lat=3.1420, lon=101.6900),
        Stop(stop_id="far", name="Far", lat=3.2000, lon=101.7500),
        Stop(stop_id="nocoords", name="None"),
    ])
    session.commit()

    results = ETAPredictor().get_nearby_stops(session, lat=3.1400, lon=101.6900, radius_m=500.0)

    assert [r.stop_id for r in results] == ["near", "mid"]
    assert results[0].distance_m == 0.0


def _seed_stop_with_trip(session: Session, stop_id: str, trip_id: str) -> None:
    stop = Stop(stop_id=stop_id, name="Window Stop")
    trip = Trip(trip_id=trip_id, route_id="route-w")
    session.add_all([stop, trip])
    session.flush()
    session.add(
        StopTime(
            trip_id_ref=trip.id, stop_id_ref=stop.id, stop_sequence=1,
            arrival_time="23:59:00", departure_time="23:59:00",
            arrival_seconds=86340, departure_seconds=86340,
        )
    )


def test_stale_trip_updates_are_ignored() -> None:
    session = build_session()
    _seed_stop_with_trip(session, "2001", "trip-stale")
    session.add(
        TripUpdateState(
            trip_id="trip-stale", route_id="route-w", stop_id="2001",
            arrival_time_unix=int(datetime.now(timezone.utc).timestamp()) + 600,
            observed_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
    )
    session.commit()

    items = ETAPredictor().get_stop_etas(session, "2001")

    assert items and items[0].prediction_source == "schedule"


def test_service_ids_are_cached_between_requests() -> None:
    session = build_session()
    predictor = ETAPredictor()
    now = datetime.now(timezone.utc)
    calls = []
    original = predictor._load_valid_service_ids
    predictor._load_valid_service_ids = lambda *a: calls.append(1) or original(*a)  # type: ignore[method-assign]

    predictor._valid_service_ids(session, now)
    predictor._valid_service_ids(session, now)

    assert len(calls) == 1


def test_batch_stop_etas_returns_empty_items_for_stops_without_data(monkeypatch) -> None:  # noqa: ANN001
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool

    from knightbus import main as main_module
    from knightbus.db import get_db
    from knightbus.schemas import EtaItem

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    session.add(Stop(stop_id="1001", name="KL105 LRT MASJID JAMEK", lat=3.149, lon=101.696))
    session.commit()
    item = EtaItem(route_id="r", trip_id="t", stop_id="1001", prediction_source="schedule", data_timestamp=datetime.now(timezone.utc))
    monkeypatch.setattr(
        main_module.predictor, "get_stop_etas", lambda db, stop_id, route_id=None: [item] if stop_id == "1001" else []
    )
    main_module.app.dependency_overrides[get_db] = lambda: session
    try:
        client = TestClient(main_module.app)
        body = client.get("/v1/stops/etas", params={"stop_ids": "1001,2002,1001"}).json()
        assert [b["stop_id"] for b in body] == ["1001", "2002"]  # order kept, duplicates dropped
        assert body[0]["stop_code"] == "KL105" and len(body[0]["items"]) == 1
        assert body[1]["items"] == []
        assert client.get("/v1/stops/etas", params={"stop_ids": ""}).status_code == 422
        too_many = ",".join(str(i) for i in range(11))
        assert client.get("/v1/stops/etas", params={"stop_ids": too_many}).status_code == 422
        assert client.get("/v1/stops/9999/etas").status_code == 404
    finally:
        main_module.app.dependency_overrides.clear()


def _freeze_time(monkeypatch, local_hour: int = 12):
    from zoneinfo import ZoneInfo

    now = datetime(2026, 11, 15, local_hour, tzinfo=ZoneInfo("Asia/Kuala_Lumpur")).astimezone(timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(predictor_module, "datetime", FrozenDateTime)
    return now


def test_no_arrivals_after_last_bus_even_with_parked_gps_and_ml(monkeypatch):
    now = _freeze_time(monkeypatch, 23)
    session = build_session()
    stop, _ = _stop_with_trip(session, "finished", {1: 6 * 3600, 2: 22 * 3600})
    session.add(VehicleState(lat=3.02, lon=101.71, vehicle_id="parked", trip_id="finished", current_stop_sequence=1,
                             timestamp_unix=int(now.timestamp()), observed_at=now))
    session.commit()
    predictor = ETAPredictor()
    monkeypatch.setattr(predictor, "current_model", lambda: StubModel(100))
    assert predictor.get_stop_etas(session, stop.stop_id) == []


def test_before_service_only_timetable_despite_parked_gps_and_ml(monkeypatch):
    now = _freeze_time(monkeypatch, 3)
    session = build_session()
    stop, _ = _stop_with_trip(session, "morning", {1: 6 * 3600, 2: 7 * 3600})
    session.add(VehicleState(lat=3.02, lon=101.71, vehicle_id="parked", trip_id="morning", current_stop_sequence=1,
                             timestamp_unix=int(now.timestamp()), observed_at=now))
    session.commit()
    predictor = ETAPredictor()
    monkeypatch.setattr(predictor, "current_model", lambda: StubModel(100))
    items = predictor.get_stop_etas(session, stop.stop_id)
    assert items and all(it.prediction_source == "schedule" and it.vehicle_id is None for it in items)
    assert items[0].predicted_arrival_time.hour == 22  # 06:00 MYT, UTC previous day


def test_delayed_bus_included_and_sorted_by_live_arrival(monkeypatch):
    now = _freeze_time(monkeypatch)
    session = build_session()
    # Its scheduled arrival is already past, but this bus is still approaching.
    stop, trip = _stop_with_trip(session, "late", {1: 11 * 3600, 2: 11 * 3600 + 600})
    end = Stop(stop_id="end", name="End")
    session.add(end)
    session.flush()
    session.add(StopTime(trip_id_ref=trip.id, stop_id_ref=end.id, stop_sequence=3, arrival_seconds=13 * 3600))
    session.add(VehicleState(lat=3.02, lon=101.71, vehicle_id="late-bus", trip_id="late", current_stop_sequence=1,
                             timestamp_unix=int(now.timestamp()), observed_at=now))
    session.commit()
    items = ETAPredictor().get_stop_etas(session, stop.stop_id)
    assert [(it.vehicle_id, it.minutes_away) for it in items] == [("late-bus", 0), ("late-bus", 10)]


def test_stale_gps_cannot_become_live_from_fresh_fetch(monkeypatch):
    now = _freeze_time(monkeypatch)
    session = build_session()
    stop, _ = _stop_with_trip(session, "old-gps", {1: 11 * 3600, 2: 13 * 3600})
    session.add(VehicleState(lat=3.02, lon=101.71, vehicle_id="old", trip_id="old-gps", current_stop_sequence=1,
                             timestamp_unix=int((now - timedelta(minutes=30)).timestamp()), observed_at=now))
    session.commit()
    items = ETAPredictor().get_stop_etas(session, stop.stop_id)
    assert items and all(it.prediction_source == "schedule" and it.vehicle_id is None for it in items)


def test_latest_vehicle_position_supersedes_old_trip_and_stop_match(monkeypatch):
    now = _freeze_time(monkeypatch)
    session = build_session()
    stop, _ = _stop_with_trip(session, "old-trip", {1: 11 * 3600, 2: 13 * 3600})
    session.add_all([
        VehicleState(lat=3.02, lon=101.71, vehicle_id="bus", trip_id="old-trip", current_stop_sequence=1,
                     timestamp_unix=int(now.timestamp()) - 30, observed_at=now - timedelta(seconds=30)),
        VehicleState(lat=3.02, lon=101.71, vehicle_id="bus", trip_id="new-trip", current_stop_sequence=None,
                     timestamp_unix=int(now.timestamp()), observed_at=now),
    ])
    session.commit()
    items = ETAPredictor().get_stop_etas(session, stop.stop_id)
    assert items and all(it.vehicle_id is None for it in items)


def test_expired_trip_update_is_not_clamped_to_now(monkeypatch):
    now = _freeze_time(monkeypatch)
    session = build_session()
    stop, _ = _stop_with_trip(session, "expired", {1: 11 * 3600, 2: 13 * 3600})
    session.add(TripUpdateState(trip_id="expired", stop_id=stop.stop_id,
                               arrival_time_unix=int(now.timestamp()) - 60, observed_at=now))
    session.commit()
    assert ETAPredictor().get_stop_etas(session, stop.stop_id) == []
