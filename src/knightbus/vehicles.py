from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from knightbus.models import Stop, Trip, VehicleState
from knightbus.positions import latest_position_ids
from knightbus.schemas import VehicleResponse


def route_vehicle_rows(db: Session, route_id: str, feed_name: str | None = None,
                       now: datetime | None = None) -> list[tuple[VehicleState, str | None, int | None, str | None]]:
    """Return the actual selected GPS records and their static metadata."""
    query = (
        select(VehicleState, Trip.headsign, Trip.direction_id, Stop.name)
        .where(VehicleState.id.in_(latest_position_ids(now or datetime.now(timezone.utc), 30 * 60)))
        .outerjoin(Trip, and_(Trip.feed_name == VehicleState.feed_name, Trip.trip_id == VehicleState.trip_id))
        .outerjoin(Stop, and_(Stop.feed_name == VehicleState.feed_name, Stop.stop_id == VehicleState.stop_id))
        .where(or_(VehicleState.route_id == route_id, Trip.route_id == route_id))
        .order_by(VehicleState.observed_at.desc())
        .limit(100)
    )
    if feed_name is not None:
        query = query.where(VehicleState.feed_name == feed_name)
    return [(v, headsign, direction, name) for v, headsign, direction, name in db.execute(query)]


def route_vehicles(db: Session, route_id: str, feed_name: str | None = None) -> list[VehicleResponse]:
    return [
        VehicleResponse(
            position_source=v.position_source,
            vehicle_id=v.vehicle_id, route_id=v.route_id, trip_id=v.trip_id,
            stop_id=v.stop_id, current_stop_sequence=v.current_stop_sequence,
            lat=v.lat, lon=v.lon,
            observed_at=(datetime.fromtimestamp(v.timestamp_unix, timezone.utc)
                         if v.timestamp_unix is not None else v.observed_at),
            headsign=headsign, direction_id=direction, stop_name=stop_name,
        )
        for v, headsign, direction, stop_name in route_vehicle_rows(db, route_id, feed_name)
    ]
