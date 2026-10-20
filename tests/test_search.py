from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from knightbus.db import Base, get_db
from knightbus.main import app
from knightbus.models import Route, Stop, StopTime, Trip
from knightbus.search import search


def build_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def add_stops(session: Session, rows: list[tuple[str, str, float, float]]) -> None:
    session.add_all(
        [Stop(stop_id=sid, name=name, lat=lat, lon=lon) for sid, name, lat, lon in rows]
    )
    session.commit()


def test_short_query_returns_empty() -> None:
    session = build_session()
    add_stops(session, [("1", "KL105 LRT MASJID JAMEK", 3.149, 101.696)])

    for q in ("", " ", "k", " m "):
        result = search(session, q)
        assert result.stops == [] and result.routes == []


def test_code_split_and_exact_code_ranks_first() -> None:
    session = build_session()
    add_stops(
        session,
        [
            ("1", "KL1050 PASAR KL105 SENTRAL", 3.1, 101.6),
            ("2", "KL105 LRT MASJID JAMEK", 3.149, 101.696),
            ("3", "KL1051 KL105 TAMAN", 3.2, 101.7),
        ],
    )

    result = search(session, "kl105")

    assert [s.stop_id for s in result.stops][0] == "2"
    first = result.stops[0]
    assert first.code == "KL105"
    assert first.name == "LRT MASJID JAMEK"
    assert first.distance_m is None


def test_name_without_code_keeps_full_name() -> None:
    session = build_session()
    add_stops(session, [("1", "Jalan Ampang", 3.1, 101.7)])

    stop = search(session, "ampang").stops[0]

    assert stop.code is None
    assert stop.name == "Jalan Ampang"


def test_prefix_beats_word_beats_substring() -> None:
    session = build_session()
    add_stops(
        session,
        [
            ("sub", "KL1 PUSAT MASJIDAN", 3.1, 101.7),  # substring only
            ("word", "KL2 LRT MASJID JAMEK", 3.1, 101.7),  # word start
            ("pre", "KL3 MASJID NEGARA", 3.1, 101.7),  # name starts with q
        ],
    )

    result = search(session, "masjid")

    assert [s.stop_id for s in result.stops] == ["pre", "word", "sub"]


def test_distance_orders_within_tier_when_coordinates_given() -> None:
    session = build_session()
    add_stops(
        session,
        [
            ("far", "KL1 MASJID A", 3.30, 101.70),
            ("near", "KL2 MASJID B", 3.15, 101.70),
            ("mid", "KL3 MASJID C", 3.20, 101.70),
        ],
    )

    by_distance = search(session, "masjid", lat=3.149, lon=101.70)
    by_name = search(session, "masjid")

    assert [s.stop_id for s in by_distance.stops] == ["near", "mid", "far"]
    assert by_distance.stops[0].distance_m is not None
    assert by_distance.stops[0].distance_m < by_distance.stops[1].distance_m
    assert [s.stop_id for s in by_name.stops] == ["far", "near", "mid"]


def test_routes_exact_before_prefix_before_long_name_and_feeds_kept() -> None:
    session = build_session()
    session.add_all(
        [
            Route(feed_name="rapid-bus-kl", route_id="R1", short_name="1900", long_name="Other"),
            Route(feed_name="rapid-bus-kl", route_id="U1900", short_name="190", long_name="Desa Sri Hartamas"),
            Route(feed_name="rapid-bus-mrtfeeder", route_id="F190", short_name="190", long_name="Feeder"),
            Route(feed_name="rapid-bus-kl", route_id="R3", short_name="T1", long_name="Via 190 Plaza"),
        ]
    )
    session.commit()

    result = search(session, "190")

    assert [(r.route_id, r.feed_name) for r in result.routes] == [
        ("F190", "rapid-bus-mrtfeeder"),
        ("U1900", "rapid-bus-kl"),
        ("R1", "rapid-bus-kl"),
        ("R3", "rapid-bus-kl"),
    ]
    assert result.routes[1].short_name == "190"


def test_stop_routes_aggregated_sorted_and_capped_at_six() -> None:
    session = build_session()
    stop = Stop(stop_id="1", name="KL1 HUB", lat=3.1, lon=101.7)
    other = Stop(stop_id="2", name="KL2 HUB TWO", lat=3.2, lon=101.7)
    session.add_all([stop, other])
    session.flush()
    names = ["300", "180", "190", "180", "T10", "T2", "400", "500"]
    for i, short in enumerate(names):
        route = Route(route_id=f"R{i}", short_name=short)
        trip = Trip(trip_id=f"t{i}", route_id=f"R{i}")
        session.add_all([route, trip])
        session.flush()
        session.add(StopTime(trip_id_ref=trip.id, stop_id_ref=stop.id, stop_sequence=1))
    trip_o = Trip(trip_id="to", route_id="R0")
    session.add(trip_o)
    session.flush()
    session.add(StopTime(trip_id_ref=trip_o.id, stop_id_ref=other.id, stop_sequence=1))
    session.commit()

    result = search(session, "hub")
    by_id = {s.stop_id: s for s in result.stops}

    assert by_id["1"].routes == ["180", "190", "300", "400", "500", "T2"]
    assert by_id["2"].routes == ["300"]


def test_limit_applies_to_each_list() -> None:
    session = build_session()
    add_stops(session, [(str(i), f"KL{i} MASJID {i}", 3.1, 101.7) for i in range(10)])
    session.add_all([Route(route_id=f"R{i}", short_name=f"M{i}", long_name="Masjid loop") for i in range(10)])
    session.commit()

    result = search(session, "masjid", limit=3)

    assert len(result.stops) == 3
    assert len(result.routes) == 3


def test_like_wildcards_are_literal() -> None:
    session = build_session()
    add_stops(session, [("1", "KL1 MASJID", 3.1, 101.7)])

    assert search(session, "%%").stops == []


def test_search_endpoint() -> None:
    session = build_session()
    add_stops(session, [("1", "KL105 LRT MASJID JAMEK", 3.149, 101.696)])
    session.add(Route(feed_name="rapid-bus-kl", route_id="U1900", short_name="190", long_name="Desa"))
    session.commit()

    app.dependency_overrides[get_db] = lambda: session
    try:
        client = TestClient(app)
        body = client.get("/v1/search", params={"q": "  Masjid ", "lat": 3.149, "lon": 101.696}).json()
        assert body["query"] == "Masjid"
        assert body["stops"][0]["code"] == "KL105"
        assert body["stops"][0]["distance_m"] < 100
        assert client.get("/v1/search", params={"q": "x"}).json()["stops"] == []
        assert client.get("/v1/search", params={"q": "190"}).json()["routes"][0]["route_id"] == "U1900"
        assert client.get("/v1/search", params={"q": "masjid", "limit": 99}).status_code == 422
    finally:
        app.dependency_overrides.clear()
