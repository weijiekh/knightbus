import logging
from math import cos, radians
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Select, desc, select
from sqlalchemy.orm import Session

from knightbus.config import get_settings
from knightbus.features import build_feature_row
from knightbus.ml import ResidualModel, load_residual_model
from knightbus.models import CalendarDate, ModelVersion, Route, ServiceCalendar, Stop, StopTime, Trip, TripUpdateState, VehicleState
from knightbus.schemas import EtaItem, ModelInfoResponse, StopResponse
from knightbus.utils import delay_from_vehicle_progress, haversine_meters, scheduled_seconds_to_datetime

logger = logging.getLogger(__name__)


UPCOMING_GRACE_SECONDS = 120
VEHICLE_MAX_AGE_SECONDS = 15 * 60
# Trip updates are re-reported every poll; older rows are stale predictions. Bounding the
# lookup keeps it off the 14 days of history kept for training.
TRIP_UPDATE_MAX_AGE_SECONDS = 30 * 60


class ETAPredictor:
    def __init__(self) -> None:
        settings = get_settings()
        self._model_path = Path(settings.model_artifact_path)
        self._model_mtime: float | None = None
        self._ml_model: ResidualModel | None = None
        self._service_tz = settings.gtfs_service_tzinfo
        self.current_model()

    def current_model(self) -> ResidualModel | None:
        """Return the ML model, reloading it when the artifact file's mtime changes.

        A failed reload keeps the previously loaded model; a missing file clears it.
        """
        try:
            mtime = self._model_path.stat().st_mtime
        except FileNotFoundError:
            self._ml_model = None
            self._model_mtime = None
            return None
        except OSError:
            logger.warning("Cannot stat model artifact %s; keeping current model", self._model_path, exc_info=True)
            return self._ml_model
        if mtime != self._model_mtime:
            self._model_mtime = mtime
            loaded = load_residual_model(str(self._model_path))
            if loaded is not None:
                self._ml_model = loaded
        return self._ml_model

    def get_stop_etas(self, db: Session, stop_id: str, route_id: str | None = None) -> list[EtaItem]:
        now = datetime.now(timezone.utc)
        ml_model = self.current_model()
        stop = db.execute(select(Stop).where(Stop.stop_id == stop_id)).scalars().first()
        if stop is None:
            return []

        query: Select[tuple[StopTime, Trip, Route]] = (
            select(StopTime, Trip, Route)
            .join(Trip, StopTime.trip_id_ref == Trip.id)
            .outerjoin(Route, (Route.route_id == Trip.route_id) & (Route.feed_name == Trip.feed_name))
            .where(StopTime.stop_id_ref == stop.id)
            .order_by(StopTime.arrival_seconds.asc())
        )
        if route_id:
            query = query.where(Trip.route_id == route_id)

        valid_sids = self._valid_service_ids(db, now)
        if valid_sids is not None:
            query = query.where(Trip.service_id.in_(valid_sids))

        # Upcoming first. Past the last bus of the day the lookup falls back to
        # the day's earliest trips, which the UI shows as "First bus".
        local_now = now.astimezone(self._service_tz)
        now_seconds = local_now.hour * 3600 + local_now.minute * 60 + local_now.second
        schedule_rows = db.execute(
            query.where(StopTime.arrival_seconds >= now_seconds - UPCOMING_GRACE_SECONDS).limit(25)
        ).all()
        if not schedule_rows:
            schedule_rows = db.execute(query.limit(25)).all()
        latest_updates = self._latest_trip_updates(db, stop_id, now)
        latest_vehicles = self._latest_vehicles(db, [trip.trip_id for _, trip, _ in schedule_rows], now)
        trip_schedule_seconds = self._trip_schedule_seconds(db, [trip.id for _, trip, _ in schedule_rows])

        items: list[EtaItem] = []
        for stop_time, trip, route in schedule_rows:
            scheduled_arrival = scheduled_seconds_to_datetime(now, stop_time.arrival_seconds, self._service_tz)
            trip_update = latest_updates.get(trip.trip_id)
            vehicle = latest_vehicles.get(trip.trip_id)
            if (
                vehicle is not None
                and vehicle.current_stop_sequence is not None
                and vehicle.current_stop_sequence > stop_time.stop_sequence
            ):
                continue  # this bus has already passed the stop
            delay_seconds = trip_update.delay_seconds if trip_update and trip_update.delay_seconds is not None else None

            if delay_seconds is None and vehicle:
                delay_seconds = delay_from_vehicle_progress(
                    now=now,
                    vehicle=vehicle,
                    trip_schedule_seconds=trip_schedule_seconds.get(trip.id, {}),
                )

            if trip_update and trip_update.arrival_time_unix:
                predicted_arrival = datetime.fromtimestamp(trip_update.arrival_time_unix, tz=timezone.utc)
                source = "heuristic"
            elif scheduled_arrival:
                predicted_arrival = scheduled_arrival.replace(tzinfo=timezone.utc)
                if delay_seconds is not None:
                    predicted_arrival = predicted_arrival.fromtimestamp(
                        predicted_arrival.timestamp() + delay_seconds,
                        tz=timezone.utc,
                    )
                source = "schedule" if delay_seconds is None else "heuristic"
            else:
                predicted_arrival = None
                source = "schedule"

            if (
                predicted_arrival
                and ml_model is not None
                and not (trip_update and trip_update.arrival_time_unix)
            ):
                feature_row = build_feature_row(
                    now=now,
                    stop=stop,
                    stop_time=stop_time,
                    trip=trip,
                    vehicle=vehicle,
                    delay_seconds=delay_seconds,
                )
                if feature_row.baseline_eta_seconds is not None:
                    residual_seconds = ml_model.predict(feature_row.features)
                    adjusted_seconds = max(0, int(feature_row.baseline_eta_seconds + residual_seconds))
                    predicted_arrival = now + timedelta(seconds=adjusted_seconds)
                    source = "ml_residual"

            minutes_away = None
            if predicted_arrival:
                minutes_away = max(0, int((predicted_arrival - now).total_seconds() // 60))

            items.append(
                EtaItem(
                    stop_id=stop_id,
                    route_id=trip.route_id,
                    route_short_name=route.short_name if route else None,
                    route_long_name=route.long_name if route else None,
                    trip_id=trip.trip_id,
                    headsign=trip.headsign,
                    vehicle_id=vehicle.vehicle_id if vehicle else (trip_update.vehicle_id if trip_update else None),
                    predicted_arrival_time=predicted_arrival,
                    scheduled_arrival_time=scheduled_arrival,
                    minutes_away=minutes_away,
                    prediction_source=source,
                    data_timestamp=(trip_update.observed_at if trip_update else vehicle.observed_at if vehicle else now),
                    delay_seconds=delay_seconds,
                )
            )

        return [item for item in items if item.predicted_arrival_time is not None][:10]

    def list_route_vehicles(self, db: Session, route_id: str) -> list[VehicleState]:
        query = (
            select(VehicleState)
            .where(VehicleState.route_id == route_id)
            .order_by(desc(VehicleState.observed_at))
            .limit(100)
        )
        return list(db.execute(query).scalars().all())

    def _valid_service_ids(self, db: Session, now: datetime) -> set[str] | None:
        """Return service_ids valid for today, or None if no calendar data exists."""
        local_now = now.astimezone(self._service_tz)
        today_str = local_now.strftime("%Y%m%d")
        day_col = local_now.strftime("%A").lower()  # e.g. "monday"
        # Check if any calendar data exists
        has_calendar = db.execute(select(ServiceCalendar).limit(1)).scalars().first()
        if has_calendar is None:
            return None

        day_filter = getattr(ServiceCalendar, day_col) == True  # noqa: E712
        rows = db.execute(
            select(ServiceCalendar.service_id).where(
                day_filter,
                ServiceCalendar.start_date <= today_str,
                ServiceCalendar.end_date >= today_str,
            )
        ).scalars().all()
        valid = set(rows)

        # Apply calendar_dates exceptions
        exceptions = db.execute(
            select(CalendarDate.service_id, CalendarDate.exception_type).where(
                CalendarDate.date == today_str
            )
        ).all()
        for service_id, exception_type in exceptions:
            if exception_type == 1:
                valid.add(service_id)
            elif exception_type == 2:
                valid.discard(service_id)

        return valid

    def get_nearby_stops(
        self, db: Session, lat: float, lon: float, radius_m: float = 500.0, limit: int = 5
    ) -> list[StopResponse]:
        # Bounding box in SQL, narrow columns: loading every Stop as an ORM
        # object per request was the API's biggest allocation.
        lat_delta = radius_m / 111_320.0
        lon_delta = lat_delta / max(cos(radians(lat)), 0.01)
        stops = db.execute(
            select(Stop.stop_id, Stop.name, Stop.lat, Stop.lon).where(
                Stop.lat.between(lat - lat_delta, lat + lat_delta),
                Stop.lon.between(lon - lon_delta, lon + lon_delta),
            )
        ).all()
        results = []
        for stop_id, name, stop_lat, stop_lon in stops:
            dist = haversine_meters(lat, lon, stop_lat, stop_lon)
            if dist <= radius_m:
                results.append(StopResponse(
                    stop_id=stop_id,
                    name=name,
                    lat=stop_lat,
                    lon=stop_lon,
                    distance_m=round(dist, 1),
                ))
        results.sort(key=lambda s: s.distance_m or 0)
        return results[:limit]

    def current_model_info(self, db: Session) -> ModelInfoResponse:
        version = db.execute(select(ModelVersion).order_by(desc(ModelVersion.created_at)).limit(1)).scalars().first()
        if version is None:
            return ModelInfoResponse(status="not_trained")
        return ModelInfoResponse(version=version.version, status=version.status, metrics_json=version.metrics_json)

    @staticmethod
    def _latest_trip_updates(db: Session, stop_id: str, now: datetime) -> dict[str, TripUpdateState]:
        rows = (
            db.execute(
                select(TripUpdateState)
                .where(
                    TripUpdateState.stop_id == stop_id,
                    TripUpdateState.observed_at >= now - timedelta(seconds=TRIP_UPDATE_MAX_AGE_SECONDS),
                )
                .order_by(desc(TripUpdateState.observed_at))
                .limit(200)
            )
            .scalars()
            .all()
        )
        latest: dict[str, TripUpdateState] = {}
        for row in rows:
            latest.setdefault(row.trip_id, row)
        return latest

    @staticmethod
    def _latest_vehicles(db: Session, trip_ids: list[str], now: datetime) -> dict[str, VehicleState]:
        """Latest stop-matched position of each trip's bus, wherever it is on the route."""
        if not trip_ids:
            return {}
        rows = (
            db.execute(
                select(VehicleState)
                .where(
                    VehicleState.trip_id.in_(trip_ids),
                    VehicleState.current_stop_sequence.is_not(None),
                    VehicleState.observed_at >= now - timedelta(seconds=VEHICLE_MAX_AGE_SECONDS),
                )
                .order_by(desc(VehicleState.observed_at))
                .limit(500)
            )
            .scalars()
            .all()
        )
        latest: dict[str, VehicleState] = {}
        for row in rows:
            latest.setdefault(row.trip_id, row)
        return latest

    @staticmethod
    def _trip_schedule_seconds(db: Session, trip_ids: list[int]) -> dict[int, dict[int, int | None]]:
        if not trip_ids:
            return {}

        rows = (
            db.execute(
                select(StopTime.trip_id_ref, StopTime.stop_sequence, StopTime.arrival_seconds).where(
                    StopTime.trip_id_ref.in_(trip_ids)
                )
            )
            .all()
        )
        schedule: dict[int, dict[int, int | None]] = {}
        for trip_id_ref, stop_sequence, arrival_seconds in rows:
            schedule.setdefault(trip_id_ref, {})[stop_sequence] = arrival_seconds
        return schedule
