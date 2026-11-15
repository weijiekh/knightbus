"""Select one valid GPS observation per vehicle across realtime sources."""
from datetime import datetime, timedelta

from sqlalchemy import Select, func, or_, select

from knightbus.config import get_settings
from knightbus.models import VehicleState


def latest_position_ids(now: datetime, max_age_seconds: int) -> Select[tuple[int]]:
    cutoff = now - timedelta(seconds=max_age_seconds)
    # A source's latest unmatched/invalid row must supersede its older stop match.
    allowed_sources = ["gtfs", "kiosk"] if get_settings().kiosk_avl_mode == "supplement" else ["gtfs"]
    per_source = select(
        VehicleState.id,
        func.row_number().over(
            partition_by=(VehicleState.feed_name, VehicleState.vehicle_id, VehicleState.position_source),
            order_by=(VehicleState.observed_at.desc(), VehicleState.id.desc()),
        ).label("source_rank"),
    ).where(VehicleState.observed_at >= cutoff, VehicleState.position_source.in_(allowed_sources)).subquery()
    # Compare GPS time, not HTTP download time. Equal GPS timestamps prefer GTFS.
    merged = select(
        VehicleState.id,
        func.row_number().over(
            partition_by=(VehicleState.feed_name, VehicleState.vehicle_id),
            order_by=(
                func.coalesce(VehicleState.timestamp_unix, func.extract("epoch", VehicleState.observed_at)).desc(),
                (VehicleState.position_source == "gtfs").desc(),
                VehicleState.id.desc(),
            ),
        ).label("vehicle_rank"),
    ).join(per_source, per_source.c.id == VehicleState.id).where(
        per_source.c.source_rank == 1,
        VehicleState.observed_at <= now + timedelta(seconds=30),
        or_(VehicleState.timestamp_unix.is_(None),
            VehicleState.timestamp_unix.between(int(cutoff.timestamp()), int(now.timestamp()) + 30)),
        VehicleState.lat.between(-90, 90),
        VehicleState.lon.between(-180, 180),
        or_(VehicleState.lat != 0, VehicleState.lon != 0),
    ).subquery()
    return select(merged.c.id).where(merged.c.vehicle_rank == 1)
