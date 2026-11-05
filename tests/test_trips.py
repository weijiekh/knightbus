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


def _live_session():
    from datetime import datetime, timedelta, timezone
    from knightbus.models import VehicleState

    db = _session()
    now = datetime.now(timezone.utc)
    stops = [Stop(stop_id=f's{i}', name=f'Stop {i}') for i in range(4)]
    trips = [Trip(trip_id=tid, route_id='r', direction_id=direction) for tid, direction in [('t', 0), ('later', 0), ('reverse', 1)]]
    db.add_all([Route(route_id='r', short_name='190'), *stops, *trips])
    db.flush()
    for trip in trips:
        pattern = list(reversed(stops)) if trip.direction_id == 1 else stops
        for i, stop in enumerate(pattern):
            db.add(StopTime(trip_id_ref=trip.id, stop_id_ref=stop.id, stop_sequence=(i + 1) * 10, arrival_seconds=36000 + i * 180))
    for vehicle, tid, seq, age, feed in [
        ('chosen', 't', 20, 10, 'default'), ('next', 'later', 10, 10, 'default'),
        ('opposite', 'reverse', 10, 10, 'default'), ('stale', 't', 10, 301, 'default'),
        ('foreign', 't', 10, 10, 'other'), ('unknown', 't', 999, 10, 'default'),
    ]:
        db.add(VehicleState(feed_name=feed, vehicle_id=vehicle, route_id='r', trip_id=tid, current_stop_sequence=seq, observed_at=now-timedelta(seconds=age)))
    db.commit()
    return db


def test_journey_compares_same_pattern_and_excludes_stale_other_feed_and_reverse():
    from knightbus.trips import get_trip_journey

    result = get_trip_journey(_live_session(), 't')
    buses = {b.vehicle_id: b for b in result.buses}
    assert set(buses) == {'chosen', 'next', 'unknown'}
    assert buses['chosen'].current_stop_sequence == 20
    assert set(buses['chosen'].arrivals) == {30, 40}
    assert buses['next'].arrivals[30] > buses['chosen'].arrivals[30]
    assert buses['unknown'].current_stop_sequence is None
    assert buses['unknown'].arrivals == {}


def test_journey_missing_schedule_does_not_invent_eta():
    from sqlalchemy import update
    from knightbus.trips import get_trip_journey

    db = _live_session()
    db.execute(update(StopTime).where(StopTime.stop_sequence == 30).values(arrival_seconds=None))
    db.commit()
    chosen = next(b for b in get_trip_journey(db, 't').buses if b.vehicle_id == 'chosen')
    assert chosen.current_stop_sequence == 20
    assert chosen.arrivals == {}


def test_journey_does_not_guess_repeated_loop_stop_without_sequence():
    from datetime import datetime, timezone
    from knightbus.models import VehicleState
    from knightbus.trips import get_trip_journey

    db = _live_session()
    trip = db.query(Trip).filter_by(trip_id='t').one()
    stop = db.query(Stop).filter_by(stop_id='s0').one()
    db.add(StopTime(trip_id_ref=trip.id, stop_id_ref=stop.id, stop_sequence=50, arrival_seconds=37000))
    db.add(VehicleState(feed_name='default', vehicle_id='loop', route_id='r', trip_id='t', stop_id='s0', observed_at=datetime.now(timezone.utc)))
    db.commit()
    bus = next(b for b in get_trip_journey(db, 't').buses if b.vehicle_id == 'loop')
    assert bus.current_stop_sequence is None
    assert bus.arrivals == {}


def test_route_details_returns_public_names_for_direct_links():
    from knightbus.main import route_details

    db = _session()
    db.add(Route(route_id='internal-route-123', short_name='190', long_name='Kota Damansara ~ Pasar Seni'))
    db.commit()
    result = route_details('internal-route-123', db)
    assert result.short_name == '190'
    assert result.long_name == 'Kota Damansara ~ Pasar Seni'
