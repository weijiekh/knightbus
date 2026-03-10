from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from knightbus.config import get_settings
from knightbus.models import Stop, StopTime, Trip, VehicleState
from knightbus.utils import haversine_meters, scheduled_seconds_to_datetime

FEATURE_COLUMNS = [
    "route_id",
    "stop_id",
    "stop_sequence",
    "hour_of_day",
    "day_of_week",
    "scheduled_eta_seconds",
    "delay_seconds",
    "vehicle_distance_m",
    "vehicle_time_since_update_seconds",
    "vehicle_stop_sequence",
]

CATEGORICAL_COLUMNS = ["route_id", "stop_id"]


@dataclass(frozen=True)
class FeatureRow:
    features: dict[str, object]
    baseline_eta_seconds: int | None
    scheduled_arrival: datetime | None


def build_feature_row(
    now: datetime,
    stop: Stop,
    stop_time: StopTime,
    trip: Trip,
    vehicle: VehicleState | None,
    delay_seconds: int | None,
) -> FeatureRow:
    settings = get_settings()
    local_now = now.astimezone(settings.gtfs_service_tzinfo)
    scheduled_arrival = scheduled_seconds_to_datetime(now, stop_time.arrival_seconds)
    scheduled_eta_seconds = None
    if scheduled_arrival is not None:
        scheduled_eta_seconds = int((scheduled_arrival - now).total_seconds())

    baseline_eta_seconds = None
    if scheduled_eta_seconds is not None:
        baseline_eta_seconds = scheduled_eta_seconds + (delay_seconds or 0)
        if baseline_eta_seconds < 0:
            baseline_eta_seconds = 0

    vehicle_distance_m = None
    if (
        vehicle
        and vehicle.lat is not None
        and vehicle.lon is not None
        and stop.lat is not None
        and stop.lon is not None
    ):
        vehicle_distance_m = haversine_meters(vehicle.lat, vehicle.lon, stop.lat, stop.lon)

    vehicle_time_since_update_seconds = None
    if vehicle is not None:
        vehicle_time_since_update_seconds = int((now - vehicle.observed_at).total_seconds())

    features = {
        "route_id": trip.route_id,
        "stop_id": stop.stop_id,
        "stop_sequence": stop_time.stop_sequence,
        "hour_of_day": local_now.hour,
        "day_of_week": local_now.weekday(),
        "scheduled_eta_seconds": scheduled_eta_seconds,
        "delay_seconds": delay_seconds,
        "vehicle_distance_m": vehicle_distance_m,
        "vehicle_time_since_update_seconds": vehicle_time_since_update_seconds,
        "vehicle_stop_sequence": vehicle.current_stop_sequence if vehicle else None,
    }

    return FeatureRow(
        features=features,
        baseline_eta_seconds=baseline_eta_seconds,
        scheduled_arrival=scheduled_arrival,
    )
