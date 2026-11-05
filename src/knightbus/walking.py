"""Pedestrian routing via OpenRouteService (foot-walking profile).

The API key stays server-side. Results are cached in-process so repeated
views of the same stop do not spend the free-tier quota.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from knightbus.config import get_settings
from knightbus.schemas import WalkStep

FOOT_PATH = "/v2/directions/foot-walking/geojson"
REQUEST_TIMEOUT_SECONDS = 8.0
CACHE_TTL_SECONDS = 600
CACHE_MAX_ENTRIES = 500
# Malaysia and Singapore. Requests outside this box are rejected before spending quota.
SERVICE_AREA = (0.5, 7.5, 99.0, 120.0)  # lat_min, lat_max, lon_min, lon_max


class WalkingUnavailable(Exception):
    """Routing is not configured (no API key)."""


class WalkingError(Exception):
    """Upstream routing request failed or returned an unexpected payload."""


@dataclass(frozen=True)
class WalkRoute:
    distance_m: float
    duration_s: float
    geometry: list[list[float]]  # [[lat, lon], ...]
    steps: list[WalkStep] = field(default_factory=list)


_cache: dict[tuple[float, float, float, float], tuple[float, WalkRoute]] = {}


def in_service_area(lat: float, lon: float) -> bool:
    lat_min, lat_max, lon_min, lon_max = SERVICE_AREA
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def parse_ors_geojson(payload: dict) -> WalkRoute:
    try:
        feature = payload["features"][0]
        summary = feature["properties"]["summary"]
        coords = feature["geometry"]["coordinates"]
        geometry = [[round(lat, 5), round(lon, 5)] for lon, lat in coords]
        # Older responses can contain geometry without instructions. A malformed
        # instruction list must not prevent displaying an otherwise usable route.
        try:
            steps = [
                WalkStep(
                    instruction=step["instruction"].strip(), distance_m=step["distance"], type=step.get("type"),
                    way_points=_step_way_points(step.get("way_points"), len(geometry)),
                )
                for segment in feature["properties"].get("segments", [])
                for step in segment.get("steps", [])
            ]
        except (AttributeError, KeyError, TypeError, ValueError):
            steps = []
        return WalkRoute(
            distance_m=float(summary["distance"]),
            duration_s=float(summary["duration"]),
            geometry=geometry,
            steps=steps,
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise WalkingError("unexpected routing response") from exc


def _step_way_points(value: object, geometry_size: int) -> tuple[int, int] | None:
    """Keep instructions usable when the provider omits or corrupts indices."""
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(type(index) is int for index in value)
            and 0 <= value[0] <= value[1] < geometry_size):
        return value[0], value[1]
    return None


def walking_route(from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> WalkRoute:
    settings = get_settings()
    if not settings.ors_api_key:
        raise WalkingUnavailable("walking routes not configured")

    key = (round(from_lat, 4), round(from_lon, 4), round(to_lat, 4), round(to_lon, 4))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]

    try:
        response = httpx.post(
            settings.ors_base_url.rstrip("/") + FOOT_PATH,
            json={"coordinates": [[from_lon, from_lat], [to_lon, to_lat]], "instructions": True, "language": "en"},
            headers={"Authorization": settings.ors_api_key, "Accept": "application/geo+json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise WalkingError("routing request failed") from exc
    if response.status_code != 200:
        raise WalkingError(f"routing service returned HTTP {response.status_code}")

    try:
        route = parse_ors_geojson(response.json())
    except ValueError as exc:
        raise WalkingError("unexpected routing response") from exc
    if len(_cache) >= CACHE_MAX_ENTRIES:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (now, route)
    return route
