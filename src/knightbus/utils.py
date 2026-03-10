from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Iterable

from knightbus.models import VehicleState


def parse_gtfs_time_to_seconds(value: str | None) -> int | None:
    if not value:
        return None

    parts = value.split(":")
    if len(parts) != 3:
        return None

    hours, minutes, seconds = [int(part) for part in parts]
    return hours * 3600 + minutes * 60 + seconds


def scheduled_seconds_to_datetime(reference: datetime, seconds: int | None) -> datetime | None:
    if seconds is None:
        return None

    day_start = reference.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start + timedelta(seconds=seconds)


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6371000.0
    phi1 = radians(lat1)
    phi2 = radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)

    a = sin(dphi / 2.0) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2.0) ** 2
    c = 2.0 * asin(sqrt(a))
    return radius_m * c


def delay_from_vehicle_progress(
    now: datetime,
    vehicle: VehicleState,
    trip_schedule_seconds: dict[int, int | None],
) -> int | None:
    if vehicle.current_stop_sequence is None:
        return None

    scheduled_current_seconds = trip_schedule_seconds.get(vehicle.current_stop_sequence)
    scheduled_current_time = scheduled_seconds_to_datetime(now, scheduled_current_seconds)
    if scheduled_current_time is None:
        return None

    observed_time = (
        datetime.fromtimestamp(vehicle.timestamp_unix, tz=timezone.utc)
        if vehicle.timestamp_unix is not None
        else vehicle.observed_at
    )
    return int((observed_time - scheduled_current_time).total_seconds())
