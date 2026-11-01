"""Ordered stop list for one trip, used by the step-through "next stop" view."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from knightbus.models import Route, Stop, StopTime, Trip
from knightbus.schemas import TripStop, TripStopsResponse
from knightbus.search import split_stop_name


def get_trip_stops(db: Session, trip_id: str) -> TripStopsResponse | None:
    trip = db.execute(select(Trip).where(Trip.trip_id == trip_id).order_by(Trip.id)).scalars().first()
    if trip is None:
        return None
    route_short_name = None
    if trip.route_id:
        route_short_name = db.execute(
            select(Route.short_name).where(Route.route_id == trip.route_id).order_by(Route.id)
        ).scalars().first()
    rows = db.execute(
        select(StopTime.stop_sequence, Stop.stop_id, Stop.name, Stop.lat, Stop.lon)
        .join(Stop, StopTime.stop_id_ref == Stop.id)
        .where(StopTime.trip_id_ref == trip.id)
        .order_by(StopTime.stop_sequence)
    ).all()
    stops = []
    for sequence, stop_id, raw_name, lat, lon in rows:
        code, name = split_stop_name(raw_name)
        stops.append(TripStop(stop_id=stop_id, stop_sequence=sequence, stop_code=code, stop_name=name, lat=lat, lon=lon))
    if not stops:
        return None
    return TripStopsResponse(
        trip_id=trip.trip_id,
        route_id=trip.route_id,
        route_short_name=route_short_name,
        headsign=trip.headsign,
        stops=stops,
    )
