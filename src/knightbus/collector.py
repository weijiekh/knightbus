import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from google.transit import gtfs_realtime_pb2 as gtfs_rt

from knightbus.config import FeedConfig, get_settings
from knightbus.models import CalendarDate, RawSnapshot, Route, ServiceCalendar, Stop, StopTime, Trip, TripUpdateState, VehicleState
from knightbus.utils import haversine_meters, parse_gtfs_time_to_seconds

STOP_MATCH_RADIUS_METERS = 200.0


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
        db.add(
            RawSnapshot(
                feed_type=feed_type,
                fetched_at=payload.fetched_at,
                source_url=payload.url,
                payload=payload.payload,
            )
        )
        db.commit()

    def load_static_feed(self, db: Session, payload: FeedPayload) -> None:
        self.persist_raw_snapshot(db, "static", payload)

        with zipfile.ZipFile(io.BytesIO(payload.payload)) as archive:
            routes = self._read_csv(archive, "routes.txt")
            stops = self._read_csv(archive, "stops.txt")
            trips = self._read_csv(archive, "trips.txt")
            stop_times = self._read_csv(archive, "stop_times.txt")
            calendar = self._read_csv_optional(archive, "calendar.txt")
            calendar_dates = self._read_csv_optional(archive, "calendar_dates.txt")

        # Delete only data for this feed
        trip_ids = select(Trip.id).where(Trip.feed_name == self.feed.name)
        db.execute(delete(StopTime).where(StopTime.trip_id_ref.in_(trip_ids)))
        db.execute(delete(Trip).where(Trip.feed_name == self.feed.name))
        db.execute(delete(Route).where(Route.feed_name == self.feed.name))
        db.execute(delete(Stop).where(Stop.feed_name == self.feed.name))
        db.execute(delete(ServiceCalendar).where(ServiceCalendar.feed_name == self.feed.name))
        db.execute(delete(CalendarDate).where(CalendarDate.feed_name == self.feed.name))
        db.commit()

        stop_lookup: dict[str, Stop] = {}
        trip_lookup: dict[str, Trip] = {}

        for row in routes:
            route = Route(
                feed_name=self.feed.name,
                route_id=row.get("route_id", ""),
                short_name=row.get("route_short_name"),
                long_name=row.get("route_long_name"),
                route_type=int(row["route_type"]) if row.get("route_type") else None,
            )
            db.add(route)

        for row in stops:
            stop = Stop(
                feed_name=self.feed.name,
                stop_id=row.get("stop_id", ""),
                name=row.get("stop_name"),
                lat=float(row["stop_lat"]) if row.get("stop_lat") else None,
                lon=float(row["stop_lon"]) if row.get("stop_lon") else None,
            )
            db.add(stop)
            stop_lookup[stop.stop_id] = stop

        for row in trips:
            trip = Trip(
                feed_name=self.feed.name,
                trip_id=row.get("trip_id", ""),
                route_id=row.get("route_id"),
                service_id=row.get("service_id"),
                headsign=row.get("trip_headsign"),
                direction_id=int(row["direction_id"]) if row.get("direction_id") else None,
            )
            db.add(trip)
            trip_lookup[trip.trip_id] = trip

        db.flush()

        for row in stop_times:
            trip = trip_lookup.get(row.get("trip_id", ""))
            stop = stop_lookup.get(row.get("stop_id", ""))
            if not trip or not stop:
                continue

            db.add(
                StopTime(
                    trip_id_ref=trip.id,
                    stop_id_ref=stop.id,
                    stop_sequence=int(row["stop_sequence"]),
                    arrival_time=row.get("arrival_time"),
                    departure_time=row.get("departure_time"),
                    arrival_seconds=parse_gtfs_time_to_seconds(row.get("arrival_time")),
                    departure_seconds=parse_gtfs_time_to_seconds(row.get("departure_time")),
                )
            )

        for row in calendar:
            db.add(ServiceCalendar(
                feed_name=self.feed.name,
                service_id=row.get("service_id", ""),
                monday=row.get("monday", "0") == "1",
                tuesday=row.get("tuesday", "0") == "1",
                wednesday=row.get("wednesday", "0") == "1",
                thursday=row.get("thursday", "0") == "1",
                friday=row.get("friday", "0") == "1",
                saturday=row.get("saturday", "0") == "1",
                sunday=row.get("sunday", "0") == "1",
                start_date=row.get("start_date", ""),
                end_date=row.get("end_date", ""),
            ))

        for row in calendar_dates:
            db.add(CalendarDate(
                feed_name=self.feed.name,
                service_id=row.get("service_id", ""),
                date=row.get("date", ""),
                exception_type=int(row.get("exception_type", 0)),
            ))

        db.commit()

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

        route_stops_cache: dict[str, list[tuple[str, int, float, float]]] = {}
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

            if stop_id is None and lat is not None and lon is not None and route_id:
                matched = self._match_nearest_stop(
                    db, lat, lon, route_id, route_stops_cache,
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
    def _match_nearest_stop(
        db: Session,
        lat: float,
        lon: float,
        route_id: str,
        cache: dict[str, list[tuple[str, int, float, float]]],
    ) -> tuple[str, int] | None:
        """Find the nearest stop on a route within STOP_MATCH_RADIUS_METERS."""
        if route_id not in cache:
            rows = db.execute(
                select(Stop.stop_id, StopTime.stop_sequence, Stop.lat, Stop.lon)
                .join(StopTime, StopTime.stop_id_ref == Stop.id)
                .join(Trip, Trip.id == StopTime.trip_id_ref)
                .where(Trip.route_id == route_id, Stop.lat.is_not(None), Stop.lon.is_not(None))
                .distinct()
            ).all()
            cache[route_id] = [(r[0], r[1], r[2], r[3]) for r in rows]

        best_dist = STOP_MATCH_RADIUS_METERS
        best_match: tuple[str, int] | None = None
        for stop_id, stop_seq, slat, slon in cache[route_id]:
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
    def _read_csv(archive: zipfile.ZipFile, path: str) -> list[dict[str, Any]]:
        with archive.open(path) as file_obj:
            text_file = io.TextIOWrapper(file_obj, encoding="utf-8-sig")
            return list(csv.DictReader(text_file))

    @staticmethod
    def _read_csv_optional(archive: zipfile.ZipFile, path: str) -> list[dict[str, Any]]:
        try:
            return GTFSCollector._read_csv(archive, path)
        except KeyError:
            return []
