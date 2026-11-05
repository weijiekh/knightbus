"""Ordered stops and GPS-anchored schedule estimates for a live trip."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from knightbus.models import Route, Stop, StopTime, Trip
from knightbus.schemas import TripJourneyResponse, TripLiveBus, TripStop, TripStopsResponse
from knightbus.search import split_stop_name
from knightbus.vehicles import route_vehicles


def get_trip_stops(db: Session, trip_id: str) -> TripStopsResponse | None:
    trip = db.execute(select(Trip).where(Trip.trip_id == trip_id).order_by(Trip.id)).scalars().first()
    if trip is None:
        return None
    route_short_name = None
    if trip.route_id:
        route_short_name = db.execute(
            select(Route.short_name).where(Route.route_id == trip.route_id, Route.feed_name == trip.feed_name).order_by(Route.id)
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


def get_trip_journey(db: Session, trip_id: str) -> TripJourneyResponse | None:
    """Only compare buses with the same ordered stops, never opposite directions.

    Estimates use scheduled travel intervals anchored to the last GPS observation.
    Missing/non-monotonic schedules and stale or unmappable positions have no ETA.
    This deliberately avoids inventing travel times or extrapolating old GPS.
    """
    data = get_trip_stops(db, trip_id)
    if data is None:
        return None
    trip = db.execute(select(Trip).where(Trip.trip_id == trip_id).order_by(Trip.id)).scalars().first()
    vehicles = route_vehicles(db, trip.route_id, feed_name=trip.feed_name) if trip.route_id else []
    trip_ids = {v.trip_id for v in vehicles if v.trip_id} | {trip_id}
    rows = db.execute(
        select(Trip.trip_id, StopTime, Stop.stop_id)
        .join(StopTime, StopTime.trip_id_ref == Trip.id)
        .join(Stop, Stop.id == StopTime.stop_id_ref)
        .where(Trip.feed_name == trip.feed_name, Trip.trip_id.in_(trip_ids))
        .order_by(Trip.trip_id, StopTime.stop_sequence)
    ).all()
    patterns: dict[str, list[tuple[StopTime, str]]] = {}
    for tid, stop_time, stop_id in rows:
        patterns.setdefault(tid, []).append((stop_time, stop_id))
    target = patterns.get(trip_id, [])
    target_ids = [sid for _, sid in target]
    now = datetime.now(timezone.utc)
    buses = []
    for vehicle in vehicles:
        pattern = patterns.get(vehicle.trip_id, [])
        if [sid for _, sid in pattern] != target_ids:
            continue
        observed = vehicle.observed_at.replace(tzinfo=timezone.utc) if vehicle.observed_at.tzinfo is None else vehicle.observed_at
        if not timedelta(seconds=-30) <= now - observed <= timedelta(minutes=5):
            continue
        # Sequence disambiguates loop stops. If absent, only a unique stop is safe.
        indices = [i for i, (st, sid) in enumerate(pattern)
                   if (st.stop_sequence == vehicle.current_stop_sequence if vehicle.current_stop_sequence is not None
                       else sid == vehicle.stop_id)]
        idx = indices[0] if len(indices) == 1 else None
        arrivals = {}
        if idx is not None:
            anchor = pattern[idx][0].arrival_seconds
            previous = anchor
            for i in range(idx + 1, len(pattern)):
                seconds = pattern[i][0].arrival_seconds
                if anchor is None or seconds is None or previous is None or seconds < previous:
                    break
                previous = seconds
                eta = observed + timedelta(seconds=seconds - anchor)
                if eta >= now:
                    arrivals[target[i][0].stop_sequence] = eta
        buses.append(TripLiveBus(
            vehicle_id=vehicle.vehicle_id, trip_id=vehicle.trip_id, observed_at=observed,
            current_stop_sequence=target[idx][0].stop_sequence if idx is not None else None,
            arrivals=arrivals,
        ))
    return TripJourneyResponse(**data.model_dump(), buses=buses)
