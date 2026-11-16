import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from knightbus.collector import FeedPayload
from knightbus.config import FeedConfig, Settings, get_settings
from knightbus.db import Base
from knightbus.kiosk import KioskCollector, MAX_SNAPSHOT_BYTES, parse_position
from knightbus.models import Route, Stop, StopTime, Trip, VehicleState
from knightbus.predictor import ETAPredictor
from knightbus.vehicles import route_vehicles

FEED = "rapid-bus-mrtfeeder"
NOW = datetime(2026, 11, 15, 4, tzinfo=timezone.utc)  # noon MYT


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            Route(feed_name=FEED, route_id="30000174", long_name="T565"),
            Trip(feed_name=FEED, trip_id="trip", route_id="30000174", direction_id=0),
            Stop(feed_name=FEED, stop_id="first", lat=3.02, lon=101.71),
            Stop(feed_name=FEED, stop_id="second", lat=3.03, lon=101.72),
        ])
        session.flush()
        trip = session.execute(select(Trip)).scalar_one()
        for sid, seq, seconds in (("first", 1, 11 * 3600), ("second", 2, 13 * 3600)):
            stop = session.execute(select(Stop).where(Stop.stop_id == sid)).scalar_one()
            session.add(StopTime(trip_id_ref=trip.id, stop_id_ref=stop.id, stop_sequence=seq, arrival_seconds=seconds))
        session.commit()
        yield session


def settings(mode="supplement"):
    return Settings(_env_file=None, KIOSK_AVL_MODE=mode, GTFS_FEEDS=[FeedConfig(name=FEED)])


def position(**overrides):
    row = dict(bus_no="VK2853", route="T5650", trip_no="trip", dt_gps="2026-11-15 12:00:00",
               latitude=3.02, longitude=101.71)
    return {**row, **overrides}


def payload(*rows):
    return FeedPayload("https://example.test/avl", NOW, json.dumps({"region": "rkl", "vehicles": list(rows)}).encode())


def enable(monkeypatch, mode="supplement"):
    monkeypatch.setenv("KIOSK_AVL_MODE", mode)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("changes", [
    {"dt_gps": None}, {"dt_gps": "bad"}, {"dt_gps": "2026-11-15 11:57:59"},
    {"dt_gps": "2026-11-15 12:00:31"}, {"latitude": None}, {"latitude": "NaN"},
    {"longitude": "inf"}, {"latitude": 91}, {"longitude": 181},
    {"latitude": 0, "longitude": 0}, {"bus_no": ""}, {"trip_no": {}}, {"route": None},
])
def test_invalid_positions_are_rejected(changes):
    assert parse_position(position(**changes), NOW) is None


def test_timezone_is_explicit_and_vehicle_identity_is_normalized():
    local = parse_position(position(bus_no=" vk2853 "), NOW)
    utc = parse_position(position(dt_gps=NOW.isoformat()), NOW)
    assert local == utc
    assert local.timestamp == int(NOW.timestamp())


def test_supplement_maps_trip_progress_deduplicates_and_serves_eta(db, monkeypatch):
    enable(monkeypatch)
    collector = KioskCollector(settings())
    result = collector.collect(db, payload(position()))
    assert result == {"valid": 1, "mapped": 1, "additional": 1, "newer": 0, "inserted": 1}
    row = db.execute(select(VehicleState)).scalar_one()
    assert (row.feed_name, row.route_id, row.trip_id, row.current_stop_sequence) == (FEED, "30000174", "trip", 1)
    assert row.position_source == "kiosk"
    assert collector.collect(db, payload(position()))["inserted"] == 0
    # Pin wall time across API consumers; no network or production DB dependency.
    from knightbus import predictor, vehicles
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)
    monkeypatch.setattr(predictor, "datetime", FrozenDateTime)
    monkeypatch.setattr(vehicles, "datetime", FrozenDateTime)
    buses = route_vehicles(db, "30000174")
    assert len(buses) == 1 and buses[0].position_source == "kiosk"
    items = ETAPredictor().get_stop_etas(db, "second")
    assert len(items) == 1 and items[0].vehicle_id == "VK2853"
    assert items[0].position_source == "kiosk" and items[0].minutes_away == 120


def test_unknown_trip_is_map_only_and_unknown_route_is_skipped(db):
    collector = KioskCollector(settings())
    stats = collector.collect(db, payload(position(trip_no="unknown"), position(bus_no="other", route="UNKNOWN")))
    assert stats["inserted"] == 1
    row = db.execute(select(VehicleState)).scalar_one()
    assert row.route_id == "30000174" and row.trip_id is None and row.current_stop_sequence is None


def test_loop_stop_cannot_establish_progress(db):
    trip = db.execute(select(Trip)).scalar_one()
    stop = db.execute(select(Stop).where(Stop.stop_id == "first")).scalar_one()
    db.add(StopTime(trip_id_ref=trip.id, stop_id_ref=stop.id, stop_sequence=3, arrival_seconds=14 * 3600))
    db.commit()
    KioskCollector(settings()).collect(db, payload(position()))
    assert db.execute(select(VehicleState)).scalar_one().current_stop_sequence is None


def test_shadow_compares_valid_gtfs_coverage_without_writing(db, caplog):
    db.add(VehicleState(feed_name=FEED, vehicle_id="VK2853", trip_id="trip", position_source="gtfs",
                        timestamp_unix=int(NOW.timestamp()) - 30, observed_at=NOW, lat=3.02, lon=101.71))
    db.commit()
    with caplog.at_level("INFO"):
        stats = KioskCollector(settings("shadow")).collect(db, payload(position()))
    assert stats["newer"] == 1 and stats["additional"] == 0 and stats["inserted"] == 0
    assert len(db.execute(select(VehicleState)).scalars().all()) == 1
    assert "mode=shadow" in caplog.text and "newer=1" in caplog.text


def test_disabled_never_writes_and_invalid_snapshot_fails(db):
    assert KioskCollector(settings("disabled")).collect(db, payload(position())) == {"inserted": 0}
    with pytest.raises(ValueError):
        KioskCollector(settings()).collect(db, FeedPayload("x", NOW, b'<html>bot check</html>'))
    with pytest.raises(ValueError, match="snapshot"):
        KioskCollector(settings()).collect(db, FeedPayload("x", NOW, b'{"vehicles":[]}'))


def test_ambiguous_routes_do_not_cross_feeds(db):
    db.add(Route(feed_name="rapid-bus-kl", route_id="other", short_name="T565"))
    db.commit()
    cfg = settings()
    cfg.gtfs_feeds.append(FeedConfig(name="rapid-bus-kl"))
    assert KioskCollector(cfg).collect(db, payload(position(trip_no=None)))["inserted"] == 0
    assert KioskCollector(cfg).collect(db, payload(position()))["inserted"] == 1


def test_fetch_limits_response_size_and_rejects_http_failures(monkeypatch):
    import knightbus.kiosk as kiosk
    original = httpx.Client
    def client_for(handler):
        monkeypatch.setattr(kiosk.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler)))
    client_for(lambda request: httpx.Response(200, content=b'x' * (MAX_SNAPSHOT_BYTES + 1)))
    with pytest.raises(ValueError, match="size limit"):
        KioskCollector(settings()).fetch()
    client_for(lambda request: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        KioskCollector(settings()).fetch()


@pytest.mark.parametrize("mode,expected", [("supplement", "kiosk"), ("disabled", "gtfs"), ("shadow", "gtfs")])
def test_sources_merge_by_gps_time_and_can_be_disabled_immediately(db, monkeypatch, mode, expected):
    enable(monkeypatch, mode)
    db.add_all([
        VehicleState(feed_name=FEED, vehicle_id="bus", route_id="30000174", position_source="gtfs",
                     timestamp_unix=int(NOW.timestamp()) - 30, observed_at=NOW, lat=3.02, lon=101.71),
        VehicleState(feed_name=FEED, vehicle_id="bus", route_id="30000174", position_source="kiosk",
                     timestamp_unix=int(NOW.timestamp()), observed_at=NOW - timedelta(seconds=10), lat=3.03, lon=101.72),
    ])
    db.commit()
    from knightbus.positions import latest_position_ids
    row = db.execute(select(VehicleState).where(VehicleState.id.in_(latest_position_ids(NOW, 120)))).scalar_one()
    assert row.position_source == expected


def test_equal_gps_prefers_gtfs_and_invalid_source_can_fall_back_to_other_source(db, monkeypatch):
    enable(monkeypatch)
    for source in ("gtfs", "kiosk"):
        db.add(VehicleState(feed_name=FEED, vehicle_id="bus", route_id="30000174", position_source=source,
                            timestamp_unix=int(NOW.timestamp()), observed_at=NOW, lat=3.02, lon=101.71))
    db.commit()
    from knightbus.positions import latest_position_ids
    def latest():
        return db.execute(select(VehicleState).where(VehicleState.id.in_(latest_position_ids(NOW, 120)))).scalar_one()
    assert latest().position_source == "gtfs"
    db.add(VehicleState(feed_name=FEED, vehicle_id="bus", route_id="30000174", position_source="gtfs",
                        timestamp_unix=int(NOW.timestamp()), observed_at=NOW + timedelta(seconds=1), lat=0, lon=0))
    db.commit()
    assert latest().position_source == "kiosk"


def test_one_bad_row_does_not_discard_valid_buses_and_latest_duplicate_wins(db):
    stats = KioskCollector(settings()).collect(db, payload(
        None, position(bus_no="bad", latitude=None),
        position(dt_gps="2026-11-15 11:59:30", latitude=3.021), position(),
    ))
    assert stats["valid"] == 1 and stats["inserted"] == 1
    assert db.execute(select(VehicleState)).scalar_one().timestamp_unix == int(NOW.timestamp())


def test_missing_coordinates_do_not_displace_other_valid_source(db, monkeypatch):
    enable(monkeypatch)
    db.add_all([
        VehicleState(feed_name=FEED, vehicle_id="bus", route_id="30000174", position_source="gtfs",
                     timestamp_unix=int(NOW.timestamp()), observed_at=NOW, lat=None, lon=None),
        VehicleState(feed_name=FEED, vehicle_id="bus", route_id="30000174", position_source="kiosk",
                     timestamp_unix=int(NOW.timestamp()) - 30, observed_at=NOW, lat=3.02, lon=101.71),
    ])
    db.commit()
    from knightbus.positions import latest_position_ids
    row = db.execute(select(VehicleState).where(VehicleState.id.in_(latest_position_ids(NOW, 120)))).scalar_one()
    assert row.position_source == "kiosk"


def test_fetch_reports_provider_bot_check_without_exposing_page_body(monkeypatch):
    import knightbus.kiosk as kiosk
    original = httpx.Client
    monkeypatch.setattr(kiosk.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"Content-Type": "text/html"},
                                       content=b'<html>Incapsula incident private-details</html>')
    )))
    with pytest.raises(kiosk.KioskUnavailable, match="provider bot-check page") as caught:
        KioskCollector(settings()).fetch()
    assert "private-details" not in str(caught.value)
