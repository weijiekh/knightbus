"""Optional RapidKL kiosk GPS supplement; never imports kiosk arrival estimates."""
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from knightbus.collector import FeedPayload, GTFSCollector, STOP_MATCH_RADIUS_METERS
from knightbus.config import Settings, get_settings
from knightbus.models import Route, Trip, VehicleState
from knightbus.utils import haversine_meters

logger = logging.getLogger(__name__)
KL_FEEDS = {"rapid-bus-kl", "rapid-bus-mrtfeeder"}
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_GPS_AGE_SECONDS = 120


class KioskUnavailable(ValueError):
    """The upstream endpoint returned a non-data response such as a bot check."""


@dataclass(frozen=True)
class KioskPosition:
    vehicle_id: str
    route_code: str
    trip_id: str | None
    timestamp: int
    lat: float
    lon: float


def parse_position(row: object, now: datetime) -> KioskPosition | None:
    """Reject bad rows independently, including missing GPS times and coordinates."""
    if not isinstance(row, dict):
        return None
    try:
        vehicle = row["bus_no"].strip().upper()
        route = row["route"].strip().upper()
        if not vehicle or not route or len(vehicle) > 128 or len(route) > 64:
            return None
        gps = datetime.fromisoformat(row["dt_gps"])
        if gps.tzinfo is None:
            gps = gps.replace(tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
        timestamp = int(gps.timestamp())
        age = now.timestamp() - timestamp
        lat, lon = float(row["latitude"]), float(row["longitude"])
        if not (-30 <= age <= MAX_GPS_AGE_SECONDS and isfinite(lat) and isfinite(lon)
                and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0)):
            return None
        trip = row.get("trip_no")
        if trip is not None and (not isinstance(trip, str) or len(trip) > 128):
            return None
        return KioskPosition(vehicle, route, trip or None, timestamp, lat, lon)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def route_keys(value: str | None) -> set[str]:
    value = (value or "").strip().upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z]*\d+[A-Z]?", value):
        return set()
    return {value}


def operational_keys(code: str) -> set[str]:
    # Operational AVL codes append a zero; KL numbered services also use U.
    keys = {code, code[:-1]} if code.endswith("0") else {code}
    return keys | {key[1:] for key in keys if key.startswith("U")}


class KioskCollector:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def fetch(self) -> FeedPayload:
        data = bytearray()
        with httpx.Client(timeout=self.settings.gtfs_request_timeout_seconds) as client:
            with client.stream("GET", self.settings.kiosk_avl_url, headers={"Accept": "application/json"}) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_SNAPSHOT_BYTES:
                        raise ValueError("kiosk AVL snapshot exceeds size limit")
                if "text/html" in response.headers.get("content-type", "").lower():
                    markers = (b"incapsula", b"imperva", b"hcaptcha")
                    reason = "provider bot-check page" if any(marker in data.lower() for marker in markers) else "HTML page"
                    raise KioskUnavailable(f"kiosk returned {reason} instead of JSON (HTTP {response.status_code})")
        return FeedPayload(self.settings.kiosk_avl_url, datetime.now(timezone.utc), bytes(data))

    def collect(self, db: Session, payload: FeedPayload) -> dict[str, int]:
        snapshot = json.loads(payload.payload)
        if not isinstance(snapshot, dict) or snapshot.get("region") != "rkl" or not isinstance(snapshot.get("vehicles"), list) or len(snapshot["vehicles"]) > 5000:
            raise ValueError("invalid kiosk AVL snapshot")
        if self.settings.kiosk_avl_mode == "disabled":
            return {"inserted": 0}
        feeds = {f.name for f in self.settings.resolved_feeds} & KL_FEEDS
        routes = db.execute(select(Route).where(Route.feed_name.in_(feeds))).scalars().all()
        aliases: dict[str, set[tuple[str, str]]] = {}
        for route in routes:
            for label in (route.route_id, route.short_name, route.long_name):
                for key in route_keys(label):
                    aliases.setdefault(key, set()).add((route.feed_name, route.route_id))
        parsed: dict[str, KioskPosition] = {}
        for row in snapshot["vehicles"]:
            position = parse_position(row, payload.fetched_at)
            if position and (position.vehicle_id not in parsed or parsed[position.vehicle_id].timestamp < position.timestamp):
                parsed[position.vehicle_id] = position
        trip_ids = {p.trip_id for p in parsed.values() if p.trip_id}
        trips: dict[str, list[Trip]] = {}
        for trip in db.execute(select(Trip).where(Trip.feed_name.in_(feeds), Trip.trip_id.in_(trip_ids))).scalars():
            trips.setdefault(trip.trip_id, []).append(trip)
        cutoff = payload.fetched_at - timedelta(minutes=5)
        # Shadow counts compare against GTFS independently of already-enabled kiosk data.
        gtfs = db.execute(select(VehicleState).where(
            VehicleState.feed_name.in_(feeds), VehicleState.position_source == "gtfs",
            VehicleState.observed_at >= cutoff,
        ).order_by(VehicleState.observed_at.desc(), VehicleState.id.desc())).scalars()
        gtfs_latest: dict[tuple[str, str], VehicleState] = {}
        for row in gtfs:
            gtfs_latest.setdefault((row.feed_name, row.vehicle_id), row)
        last_seen = {}
        for feed in feeds:
            last_seen[feed] = dict(db.execute(
                select(VehicleState.vehicle_id, func.max(VehicleState.timestamp_unix))
                .where(VehicleState.feed_name == feed, VehicleState.position_source == "kiosk",
                       VehicleState.observed_at >= cutoff).group_by(VehicleState.vehicle_id)
            ).all())
        stats = {"valid": len(parsed), "mapped": 0, "additional": 0, "newer": 0, "inserted": 0}
        rows = []
        stop_cache = {}
        for position in parsed.values():
            candidates = set().union(*(aliases.get(key, set()) for key in operational_keys(position.route_code)))
            matching_trips = [t for t in trips.get(position.trip_id, []) if (t.feed_name, t.route_id) in candidates]
            trip = matching_trips[0] if len(matching_trips) == 1 else None
            if trip:
                feed, route_id = trip.feed_name, trip.route_id
            elif len(candidates) == 1:
                feed, route_id = next(iter(candidates))
            else:
                continue  # Unmapped/ambiguous routes never leak into another feed.
            stats["mapped"] += 1
            previous = gtfs_latest.get((feed, position.vehicle_id))
            previous_valid = previous is not None and previous.timestamp_unix is not None and (
                -30 <= payload.fetched_at.timestamp() - previous.timestamp_unix <= MAX_GPS_AGE_SECONDS
                and previous.lat is not None and previous.lon is not None
                and -90 <= previous.lat <= 90 and -180 <= previous.lon <= 180
                and (previous.lat, previous.lon) != (0, 0)
            )
            if not previous_valid:
                stats["additional"] += 1
            elif position.timestamp > previous.timestamp_unix:
                stats["newer"] += 1
            if self.settings.kiosk_avl_mode != "supplement" or last_seen[feed].get(position.vehicle_id, -1) >= position.timestamp:
                continue
            # Route-only positions belong on the map. Only exact static trip IDs
            # can provide a direction and ordered stop sequence for an ETA.
            stop_id, sequence = None, None
            if trip:
                cache_key = (feed, trip.trip_id)
                if cache_key not in stop_cache:
                    stop_cache[cache_key] = GTFSCollector._stops_where(db, Trip.feed_name == feed, Trip.id == trip.id)
                nearby = sorted(
                    (haversine_meters(position.lat, position.lon, lat, lon), sid, seq)
                    for sid, seq, lat, lon in stop_cache[cache_key]
                )
                if nearby and nearby[0][0] <= STOP_MATCH_RADIUS_METERS:
                    # Repeated loop stops / close competing stops cannot establish progress.
                    if len(nearby) == 1 or nearby[1][0] - nearby[0][0] > 25:
                        _, stop_id, sequence = nearby[0]
            rows.append({
                "feed_name": feed, "position_source": "kiosk", "vehicle_id": position.vehicle_id,
                "route_id": route_id, "trip_id": trip.trip_id if trip else None,
                "stop_id": stop_id, "current_stop_sequence": sequence,
                "lat": position.lat, "lon": position.lon,
                "timestamp_unix": position.timestamp, "observed_at": payload.fetched_at,
            })
        if rows:
            db.execute(insert(VehicleState), rows)
            db.commit()
        stats["inserted"] = len(rows)
        logger.info("kiosk AVL mode=%s valid=%d mapped=%d additional=%d newer=%d inserted=%d",
                    self.settings.kiosk_avl_mode, *(stats[key] for key in ("valid", "mapped", "additional", "newer", "inserted")))
        return stats
