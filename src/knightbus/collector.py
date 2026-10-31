import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

import httpx
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session
from google.transit import gtfs_realtime_pb2 as gtfs_rt

from knightbus.config import FeedConfig, get_settings
from knightbus.models import CalendarDate, RawSnapshot, Route, ServiceCalendar, StaticFeedState, Stop, StopTime, Trip, TripUpdateState, VehicleState
from knightbus.utils import haversine_meters, parse_gtfs_time_to_seconds

STOP_MATCH_RADIUS_METERS = 200.0
STATIC_INSERT_BATCH_SIZE = 1000
REQUIRED_STATIC_FILES = ("routes.txt", "stops.txt", "trips.txt", "stop_times.txt")


@dataclass
class FeedPayload:
    url: str
    fetched_at: datetime
    payload: bytes


class GTFSCollector:
    def __init__(self, feed: FeedConfig | None = None) -> None:
        self.settings = get_settings()
        if feed is None:
            feed = self.settings.resolved_feeds[0]
        self.feed = feed

    def _fetch_bytes(self, url: str) -> FeedPayload:
        timeout = self.settings.gtfs_request_timeout_seconds
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            return FeedPayload(url=url, fetched_at=datetime.now(timezone.utc), payload=response.content)

    def fetch_static(self) -> FeedPayload | None:
        if not self.feed.static_url:
            return None
        return self._fetch_bytes(self.feed.static_url)

    def fetch_vehicle_positions(self) -> FeedPayload | None:
        if not self.feed.vehicle_positions_url:
            return None
        return self._fetch_bytes(self.feed.vehicle_positions_url)

    def fetch_trip_updates(self) -> FeedPayload | None:
        if not self.feed.trip_updates_url:
            return None
        return self._fetch_bytes(self.feed.trip_updates_url)

    @staticmethod
    def persist_raw_snapshot(db: Session, feed_type: str, payload: FeedPayload) -> None:
        # Nothing reads these back; storing every poll was the biggest DB write load.
        if not get_settings().persist_raw_snapshots:
            return
        db.add(
            RawSnapshot(
                feed_type=feed_type,
                fetched_at=payload.fetched_at,
                source_url=payload.url,
                payload=payload.payload,
            )
        )
        db.commit()

    def static_unchanged(self, db: Session, payload: FeedPayload) -> bool:
        """True when this exact archive was already loaded for this feed."""
        known = db.get(StaticFeedState, self.feed.name)
        return known is not None and known.sha256 == hashlib.sha256(payload.payload).hexdigest()

    def load_static_feed(self, db: Session, payload: FeedPayload) -> None:
        feed_name = self.feed.name

        # Validate before deleting so a bad archive never wipes existing data.
        with zipfile.ZipFile(io.BytesIO(payload.payload)) as archive:
            missing = [name for name in REQUIRED_STATIC_FILES if name not in archive.namelist()]
        if missing:
            raise ValueError(f"static GTFS archive missing required files: {', '.join(missing)}")

        try:
            self._replace_static_data(db, payload)
            db.merge(StaticFeedState(
                feed_name=feed_name,
                sha256=hashlib.sha256(payload.payload).hexdigest(),
                loaded_at=payload.fetched_at,
            ))
        except Exception:
            db.rollback()
            raise
        db.commit()

    def _replace_static_data(self, db: Session, payload: FeedPayload) -> None:
        feed_name = self.feed.name

        # Delete only data for this feed (same transaction as the inserts below)
        # synchronize_session=False: the default would fetch every deleted id into
        # memory (145 MB for one feed), enough to OOM the 128m worker.
        trip_ids = select(Trip.id).where(Trip.feed_name == feed_name)
        db.execute(delete(StopTime).where(StopTime.trip_id_ref.in_(trip_ids)).execution_options(synchronize_session=False))
        db.execute(delete(Trip).where(Trip.feed_name == feed_name).execution_options(synchronize_session=False))
        db.execute(delete(Route).where(Route.feed_name == feed_name).execution_options(synchronize_session=False))
        db.execute(delete(Stop).where(Stop.feed_name == feed_name).execution_options(synchronize_session=False))
        db.execute(delete(ServiceCalendar).where(ServiceCalendar.feed_name == feed_name).execution_options(synchronize_session=False))
        db.execute(delete(CalendarDate).where(CalendarDate.feed_name == feed_name).execution_options(synchronize_session=False))

        # Stream every CSV and bulk insert with Core in batches so memory stays flat.
        with zipfile.ZipFile(io.BytesIO(payload.payload)) as archive:
            self._insert_batched(db, Route, (
                {
                    "feed_name": feed_name,
                    "route_id": row.get("route_id", ""),
                    "short_name": row.get("route_short_name"),
                    "long_name": row.get("route_long_name"),
                    "route_type": int(row["route_type"]) if row.get("route_type") else None,
                }
                for row in self._read_csv(archive, "routes.txt")
            ))
            self._insert_batched(db, Stop, (
                {
                    "feed_name": feed_name,
                    "stop_id": row.get("stop_id", ""),
                    "name": row.get("stop_name"),
                    "lat": float(row["stop_lat"]) if row.get("stop_lat") else None,
                    "lon": float(row["stop_lon"]) if row.get("stop_lon") else None,
                }
                for row in self._read_csv(archive, "stops.txt")
            ))
            self._insert_batched(db, Trip, (
                {
                    "feed_name": feed_name,
                    "trip_id": row.get("trip_id", ""),
                    "route_id": row.get("route_id"),
                    "service_id": row.get("service_id"),
                    "headsign": row.get("trip_headsign"),
                    "direction_id": int(row["direction_id"]) if row.get("direction_id") else None,
                }
                for row in self._read_csv(archive, "trips.txt")
            ))

            stop_ids: dict[str, int] = dict(
                db.execute(select(Stop.stop_id, Stop.id).where(Stop.feed_name == feed_name)).tuples().all()
            )
            trip_ids_map: dict[str, int] = dict(
                db.execute(select(Trip.trip_id, Trip.id).where(Trip.feed_name == feed_name)).tuples().all()
            )

            def stop_time_rows():
                for row in self._read_csv(archive, "stop_times.txt"):
                    trip_pk = trip_ids_map.get(row.get("trip_id", ""))
                    stop_pk = stop_ids.get(row.get("stop_id", ""))
                    if trip_pk is None or stop_pk is None:
                        continue
                    yield {
                        "trip_id_ref": trip_pk,
                        "stop_id_ref": stop_pk,
                        "stop_sequence": int(row["stop_sequence"]),
                        "arrival_time": row.get("arrival_time"),
                        "departure_time": row.get("departure_time"),
                        "arrival_seconds": parse_gtfs_time_to_seconds(row.get("arrival_time")),
                        "departure_seconds": parse_gtfs_time_to_seconds(row.get("departure_time")),
                    }

            self._insert_batched(db, StopTime, stop_time_rows())
            del stop_ids, trip_ids_map

            self._insert_batched(db, ServiceCalendar, (
                {
                    "feed_name": feed_name,
                    "service_id": row.get("service_id", ""),
                    "monday": row.get("monday", "0") == "1",
                    "tuesday": row.get("tuesday", "0") == "1",
                    "wednesday": row.get("wednesday", "0") == "1",
                    "thursday": row.get("thursday", "0") == "1",
                    "friday": row.get("friday", "0") == "1",
                    "saturday": row.get("saturday", "0") == "1",
                    "sunday": row.get("sunday", "0") == "1",
                    "start_date": row.get("start_date", ""),
                    "end_date": row.get("end_date", ""),
                }
                for row in self._read_csv_optional(archive, "calendar.txt")
            ))
            self._insert_batched(db, CalendarDate, (
                {
                    "feed_name": feed_name,
                    "service_id": row.get("service_id", ""),
                    "date": row.get("date", ""),
                    "exception_type": int(row.get("exception_type", 0)),
                }
                for row in self._read_csv_optional(archive, "calendar_dates.txt")
            ))

    @staticmethod
    def _insert_batched(db: Session, model: type, rows: Iterable[dict[str, Any]]) -> None:
        batch: list[dict[str, Any]] = []
        for row in rows:
            batch.append(row)
            if len(batch) >= STATIC_INSERT_BATCH_SIZE:
                db.execute(insert(model), batch)
                batch = []
        if batch:
            db.execute(insert(model), batch)

    @staticmethod
    def latest_vehicle_timestamps(db: Session, feed_name: str, since: datetime) -> dict[str, int]:
        rows = db.execute(
            select(VehicleState.vehicle_id, func.max(VehicleState.timestamp_unix))
            .where(
                VehicleState.feed_name == feed_name,
                VehicleState.observed_at >= since,
                VehicleState.timestamp_unix.is_not(None),
            )
            .group_by(VehicleState.vehicle_id)
        ).all()
        return {vehicle_id: ts for vehicle_id, ts in rows}

    def normalize_vehicle_positions(self, db: Session, payload: FeedPayload) -> int:
        self.persist_raw_snapshot(db, "vehicle_positions", payload)
        feed = gtfs_rt.FeedMessage()
        feed.ParseFromString(payload.payload)

        route_stops_cache: dict[tuple[str, str | None, str], list[tuple[str, int, float, float]]] = {}
        last_seen = self.latest_vehicle_timestamps(
            db, self.feed.name, since=payload.fetched_at - timedelta(hours=1)
        )

        inserted = 0
        for entity in feed.entity:
            if not entity.HasField("vehicle"):
                continue
            vehicle = entity.vehicle

            vehicle_id = vehicle.vehicle.id or entity.id or "unknown"
            timestamp_unix = vehicle.timestamp or None
            if timestamp_unix is not None and last_seen.get(vehicle_id, -1) >= timestamp_unix:
                continue
            if timestamp_unix is not None:
                last_seen[vehicle_id] = timestamp_unix

            stop_id = vehicle.stop_id or None
            stop_seq = vehicle.current_stop_sequence or None
            lat = vehicle.position.latitude if vehicle.HasField("position") else None
            lon = vehicle.position.longitude if vehicle.HasField("position") else None
            route_id = vehicle.trip.route_id or None

            trip_id = vehicle.trip.trip_id or None
            if stop_id is None and lat is not None and lon is not None and (trip_id or route_id):
                matched = self._match_nearest_stop(
                    db, lat, lon, route_id, route_stops_cache,
                    feed_name=self.feed.name, trip_id=trip_id,
                )
                if matched:
                    stop_id, stop_seq = matched

            db.add(
                VehicleState(
                    feed_name=self.feed.name,
                    vehicle_id=vehicle_id,
                    trip_id=vehicle.trip.trip_id or None,
                    route_id=route_id,
                    stop_id=stop_id,
                    current_stop_sequence=stop_seq,
                    current_status=gtfs_rt.VehiclePosition.VehicleStopStatus.Name(vehicle.current_status)
                    if vehicle.current_status is not None
                    else None,
                    lat=lat,
                    lon=lon,
                    timestamp_unix=timestamp_unix,
                    observed_at=payload.fetched_at,
                )
            )
            inserted += 1

        db.commit()
        return inserted

    @staticmethod
    def _stops_where(db: Session, *conditions: Any) -> list[tuple[str, int, float, float]]:
        rows = db.execute(
            select(Stop.stop_id, StopTime.stop_sequence, Stop.lat, Stop.lon)
            .join(StopTime, StopTime.stop_id_ref == Stop.id)
            .join(Trip, Trip.id == StopTime.trip_id_ref)
            .where(*conditions, Stop.lat.is_not(None), Stop.lon.is_not(None))
            .distinct()
        ).all()
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    @staticmethod
    def _match_nearest_stop(
        db: Session,
        lat: float,
        lon: float,
        route_id: str | None,
        cache: dict[tuple[str, str | None, str], list[tuple[str, int, float, float]]],
        feed_name: str | None = None,
        trip_id: str | None = None,
    ) -> tuple[str, int] | None:
        """Find the nearest stop within STOP_MATCH_RADIUS_METERS.

        Prefers the stops of the vehicle's own trip: some feeds (penang,
        mrtfeeder) publish realtime route_ids that never appear in static
        trips.route_id, while trip_id still joins. Falls back to the route.
        """
        candidates: list[tuple[str, int, float, float]] = []
        if trip_id and feed_name:
            key = ("trip", feed_name, trip_id)
            if key not in cache:
                cache[key] = GTFSCollector._stops_where(
                    db, Trip.feed_name == feed_name, Trip.trip_id == trip_id
                )
            candidates = cache[key]
        if not candidates and route_id:
            key = ("route", None, route_id)
            if key not in cache:
                cache[key] = GTFSCollector._stops_where(db, Trip.route_id == route_id)
            candidates = cache[key]

        best_dist = STOP_MATCH_RADIUS_METERS
        best_match: tuple[str, int] | None = None
        for stop_id, stop_seq, slat, slon in candidates:
            dist = haversine_meters(lat, lon, slat, slon)
            if dist < best_dist:
                best_dist = dist
                best_match = (stop_id, stop_seq)

        return best_match

    def normalize_trip_updates(self, db: Session, payload: FeedPayload) -> int:
        self.persist_raw_snapshot(db, "trip_updates", payload)
        feed = gtfs_rt.FeedMessage()
        feed.ParseFromString(payload.payload)

        inserted = 0
        for entity in feed.entity:
            if not entity.HasField("trip_update"):
                continue

            trip_update = entity.trip_update
            for stop_update in trip_update.stop_time_update:
                db.add(
                    TripUpdateState(
                        feed_name=self.feed.name,
                        trip_id=trip_update.trip.trip_id or entity.id or "unknown",
                        route_id=trip_update.trip.route_id or None,
                        vehicle_id=trip_update.vehicle.id or None,
                        stop_id=stop_update.stop_id or None,
                        stop_sequence=stop_update.stop_sequence or None,
                        arrival_time_unix=stop_update.arrival.time or None if stop_update.HasField("arrival") else None,
                        departure_time_unix=stop_update.departure.time or None
                        if stop_update.HasField("departure")
                        else None,
                        delay_seconds=stop_update.arrival.delay or None if stop_update.HasField("arrival") else None,
                        observed_at=payload.fetched_at,
                    )
                )
                inserted += 1

        db.commit()
        return inserted

    @staticmethod
    def _read_csv(archive: zipfile.ZipFile, path: str) -> Iterator[dict[str, Any]]:
        with archive.open(path) as file_obj:
            text_file = io.TextIOWrapper(file_obj, encoding="utf-8-sig")
            yield from csv.DictReader(text_file)

    @staticmethod
    def _read_csv_optional(archive: zipfile.ZipFile, path: str) -> Iterator[dict[str, Any]]:
        if path not in archive.namelist():
            return
        yield from GTFSCollector._read_csv(archive, path)
