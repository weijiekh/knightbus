import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import gtfs_realtime_bindings.gtfs_realtime_pb2 as gtfs_rt
import httpx
from sqlalchemy import delete
from sqlalchemy.orm import Session

from knightbus.config import get_settings
from knightbus.models import RawSnapshot, Route, Stop, StopTime, Trip, TripUpdateState, VehicleState
from knightbus.utils import parse_gtfs_time_to_seconds


@dataclass
class FeedPayload:
    url: str
    fetched_at: datetime
    payload: bytes


class GTFSCollector:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _fetch_bytes(self, url: str) -> FeedPayload:
        timeout = self.settings.gtfs_request_timeout_seconds
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            return FeedPayload(url=url, fetched_at=datetime.now(timezone.utc), payload=response.content)

    def fetch_static(self) -> FeedPayload | None:
        if not self.settings.gtfs_static_url:
            return None
        return self._fetch_bytes(self.settings.gtfs_static_url)

    def fetch_vehicle_positions(self) -> FeedPayload | None:
        if not self.settings.gtfs_vehicle_positions_url:
            return None
        return self._fetch_bytes(self.settings.gtfs_vehicle_positions_url)

    def fetch_trip_updates(self) -> FeedPayload | None:
        if not self.settings.gtfs_trip_updates_url:
            return None
        return self._fetch_bytes(self.settings.gtfs_trip_updates_url)

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

    @staticmethod
    def load_static_feed(db: Session, payload: FeedPayload) -> None:
        GTFSCollector.persist_raw_snapshot(db, "static", payload)

        with zipfile.ZipFile(io.BytesIO(payload.payload)) as archive:
            routes = GTFSCollector._read_csv(archive, "routes.txt")
            stops = GTFSCollector._read_csv(archive, "stops.txt")
            trips = GTFSCollector._read_csv(archive, "trips.txt")
            stop_times = GTFSCollector._read_csv(archive, "stop_times.txt")

        db.execute(delete(StopTime))
        db.execute(delete(Trip))
        db.execute(delete(Route))
        db.execute(delete(Stop))
        db.commit()

        stop_lookup: dict[str, Stop] = {}
        trip_lookup: dict[str, Trip] = {}

        for row in routes:
            route = Route(
                route_id=row.get("route_id", ""),
                short_name=row.get("route_short_name"),
                long_name=row.get("route_long_name"),
                route_type=int(row["route_type"]) if row.get("route_type") else None,
            )
            db.add(route)

        for row in stops:
            stop = Stop(
                stop_id=row.get("stop_id", ""),
                name=row.get("stop_name"),
                lat=float(row["stop_lat"]) if row.get("stop_lat") else None,
                lon=float(row["stop_lon"]) if row.get("stop_lon") else None,
            )
            db.add(stop)
            stop_lookup[stop.stop_id] = stop

        for row in trips:
            trip = Trip(
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

        db.commit()

    @staticmethod
    def normalize_vehicle_positions(db: Session, payload: FeedPayload) -> int:
        GTFSCollector.persist_raw_snapshot(db, "vehicle_positions", payload)
        feed = gtfs_rt.FeedMessage()
        feed.ParseFromString(payload.payload)

        inserted = 0
        for entity in feed.entity:
            if not entity.HasField("vehicle"):
                continue
            vehicle = entity.vehicle
            db.add(
                VehicleState(
                    vehicle_id=vehicle.vehicle.id or entity.id or "unknown",
                    trip_id=vehicle.trip.trip_id or None,
                    route_id=vehicle.trip.route_id or None,
                    stop_id=vehicle.stop_id or None,
                    current_stop_sequence=vehicle.current_stop_sequence or None,
                    current_status=gtfs_rt.VehiclePosition.VehicleStopStatus.Name(vehicle.current_status)
                    if vehicle.current_status is not None
                    else None,
                    lat=vehicle.position.latitude if vehicle.HasField("position") else None,
                    lon=vehicle.position.longitude if vehicle.HasField("position") else None,
                    timestamp_unix=vehicle.timestamp or None,
                    observed_at=payload.fetched_at,
                )
            )
            inserted += 1

        db.commit()
        return inserted

    @staticmethod
    def normalize_trip_updates(db: Session, payload: FeedPayload) -> int:
        GTFSCollector.persist_raw_snapshot(db, "trip_updates", payload)
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

