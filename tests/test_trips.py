from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from knightbus.models import Base, Route, Stop, StopTime, Trip
from knightbus.trips import get_trip_stops


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_trip_stops_are_ordered_by_sequence_with_route_and_names() -> None:
    db = _session()
    route = Route(route_id="R1", short_name="190", long_name="A ~ B")
    trip = Trip(trip_id="T1", route_id="R1", headsign="Pasar Seni")
    stops = [
        Stop(stop_id="S2", name="KL1234 Second Stop", lat=3.2, lon=101.7),
        Stop(stop_id="S1", name="KL1111 First Stop", lat=3.1, lon=101.6),
    ]
    db.add_all([route, trip, *stops])
    db.flush()
    db.add_all([
        StopTime(trip_id_ref=trip.id, stop_id_ref=stops[1].id, stop_sequence=1),
        StopTime(trip_id_ref=trip.id, stop_id_ref=stops[0].id, stop_sequence=2),
    ])
    db.commit()

    result = get_trip_stops(db, "T1")

    assert result is not None
    assert result.route_short_name == "190"
    assert result.headsign == "Pasar Seni"
    assert [s.stop_id for s in result.stops] == ["S1", "S2"]
    assert result.stops[0].stop_code == "KL1111"


def test_unknown_trip_returns_none() -> None:
    assert get_trip_stops(_session(), "missing") is None
