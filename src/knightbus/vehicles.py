from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from knightbus.models import Stop, Trip, VehicleState
from knightbus.schemas import VehicleResponse


def route_vehicles(db: Session, route_id: str) -> list[VehicleResponse]:
    """Enrich positions with static metadata without crossing feed boundaries."""
    rows = db.execute(
        select(VehicleState, Trip.headsign, Trip.direction_id, Stop.name)
        .outerjoin(Trip, and_(Trip.feed_name == VehicleState.feed_name, Trip.trip_id == VehicleState.trip_id))
        .outerjoin(Stop, and_(Stop.feed_name == VehicleState.feed_name, Stop.stop_id == VehicleState.stop_id))
        .where(VehicleState.route_id == route_id)
        .order_by(VehicleState.observed_at.desc())
        .limit(100)
    )
    return [
        VehicleResponse(
            vehicle_id=v.vehicle_id, route_id=v.route_id, trip_id=v.trip_id,
            stop_id=v.stop_id, current_stop_sequence=v.current_stop_sequence,
            lat=v.lat, lon=v.lon, observed_at=v.observed_at,
            headsign=headsign, direction_id=direction, stop_name=stop_name,
        )
        for v, headsign, direction, stop_name in rows
    ]
