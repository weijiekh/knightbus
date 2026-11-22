"""Ordered stops and GPS-anchored schedule estimates for a live trip."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from knightbus.config import get_settings
from knightbus.models import Route, Stop, StopTime, Trip
from knightbus.schemas import TripJourneyResponse, TripLiveBus, TripStop, TripStopsResponse
from knightbus.search import split_stop_name
from knightbus.vehicles import route_vehicle_rows
from knightbus.service import route_windows, valid_service_ids
from knightbus.predictor import get_predictor
from knightbus.utils import delay_from_vehicle_progress


def get_trip_stops(db: Session, trip_id: str) -> TripStopsResponse | None:
    trip = db.execute(select(Trip).where(Trip.trip_id == trip_id).order_by(Trip.id)).scalars().first()
    if trip is None:
        return None
    route_short_name = None
    if trip.route_id:
        route = db.execute(
            select(Route).where(Route.route_id == trip.route_id, Route.feed_name == trip.feed_name).order_by(Route.id)
        ).scalars().first()
        route_short_name = (route.short_name or route.long_name) if route else None
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

    Arrival times use the same predictor and raw GPS observations as stop cards,
    including model adjustments and provider updates. Invalid schedule paths,
    stale GPS, and unmappable progress cannot invent an arrival estimate.
    """
    data = get_trip_stops(db, trip_id)
    if data is None:
        return None
    trip = db.execute(select(Trip).where(Trip.trip_id == trip_id).order_by(Trip.id)).scalars().first()
    now = datetime.now(timezone.utc)
    vehicles = [row[0] for row in route_vehicle_rows(db, trip.route_id, feed_name=trip.feed_name, now=now)] if trip.route_id else []
    trip_ids = {v.trip_id for v in vehicles if v.trip_id} | {trip_id}
    rows = db.execute(
        select(Trip, StopTime, Stop)
        .join(StopTime, StopTime.trip_id_ref == Trip.id)
        .join(Stop, Stop.id == StopTime.stop_id_ref)
        .where(Trip.feed_name == trip.feed_name, Trip.trip_id.in_(trip_ids))
        .order_by(Trip.trip_id, StopTime.stop_sequence)
    ).all()
    patterns: dict[str, list[tuple[StopTime, Stop]]] = {}
    trips = {}
    for source_trip, stop_time, stop in rows:
        patterns.setdefault(source_trip.trip_id, []).append((stop_time, stop))
        trips[source_trip.trip_id] = source_trip
    target = patterns.get(trip_id, [])
    target_ids = [stop.stop_id for _, stop in target]
    local_now = now.astimezone(get_settings().gtfs_service_tzinfo)
    local_seconds = local_now.hour * 3600 + local_now.minute * 60 + local_now.second
    active_services = []
    for offset in (0, 1):
        ids = valid_service_ids(db, local_now - timedelta(days=offset), trip.feed_name)
        bounds = route_windows(db, trip.feed_name, ids, trip.route_id).get(trip.route_id)
        if bounds and bounds[0] <= local_seconds + offset * 86400 <= bounds[1]:
            active_services.append((local_now - timedelta(days=offset), ids))
    predictor = get_predictor()
    model = predictor.current_model()
    buses = []
    for vehicle in vehicles:
        pattern = patterns.get(vehicle.trip_id, [])
        if [stop.stop_id for _, stop in pattern] != target_ids:
            continue
        observed = datetime.fromtimestamp(vehicle.timestamp_unix, timezone.utc) if vehicle.timestamp_unix is not None else vehicle.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        if not timedelta(seconds=-30) <= now - observed <= timedelta(minutes=2):
            continue
        # Sequence disambiguates loop stops. If absent, only a unique stop is safe.
        indices = [i for i, (st, stop) in enumerate(pattern)
                   if (st.stop_sequence == vehicle.current_stop_sequence if vehicle.current_stop_sequence is not None
                       else stop.stop_id == vehicle.stop_id)]
        idx = indices[0] if len(indices) == 1 else None
        arrivals, sources = {}, {}
        source_trip = trips[vehicle.trip_id]
        schedule = {st.stop_sequence: st.arrival_seconds for st, _ in pattern}
        eligible = [reference for reference, ids in active_services
                    if ids is None or source_trip.service_id in ids]
        # Match the same service day the stop predictor associates with this GPS.
        if eligible and idx is not None:
            def score(reference):
                delay = delay_from_vehicle_progress(now, vehicle, schedule, reference, get_settings().gtfs_service_tzinfo)
                return abs(delay) if delay is not None else float("inf")
            reference = min(eligible, key=score)
            predictions = predictor.predict_vehicle_arrivals(
                db, now=now, pattern=pattern, trip=source_trip, route=None,
                vehicle=vehicle, reference=reference, ml_model=model,
            )
            for i in range(idx + 1, len(pattern)):
                item = predictions.get(pattern[i][0].stop_sequence)
                if item is not None and item.prediction_source != "schedule":
                    sequence = target[i][0].stop_sequence
                    arrivals[sequence] = item.predicted_arrival_time
                    sources[sequence] = item.prediction_source
        buses.append(TripLiveBus(
            vehicle_id=vehicle.vehicle_id, trip_id=vehicle.trip_id, observed_at=observed,
            current_stop_sequence=target[idx][0].stop_sequence if idx is not None else None,
            arrivals=arrivals, arrival_sources=sources, position_source=vehicle.position_source,
        ))
    return TripJourneyResponse(**data.model_dump(), buses=buses)
