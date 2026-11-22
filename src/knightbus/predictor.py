import logging
from math import ceil, cos, radians
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from functools import lru_cache

from sqlalchemy import Select, desc, or_, select
from sqlalchemy.orm import Session

from knightbus.config import get_settings
from knightbus.features import build_feature_row
from knightbus.ml import ResidualModel, load_residual_model
from knightbus.models import ModelVersion, Route, Stop, StopTime, Trip, TripUpdateState, VehicleState
from knightbus.positions import latest_position_ids
from knightbus.service import route_windows, valid_service_ids
from knightbus.schemas import EtaItem, ModelInfoResponse, StopResponse
from knightbus.utils import delay_from_vehicle_progress, haversine_meters, scheduled_seconds_to_datetime

logger = logging.getLogger(__name__)


VEHICLE_MAX_AGE_SECONDS = 2 * 60
# Trip updates are re-reported every poll; older rows are stale predictions. Bounding the
# lookup keeps it off the 14 days of history kept for training.
TRIP_UPDATE_MAX_AGE_SECONDS = 2 * 60
# Calendar rows only change on a static GTFS reload (daily), so per-request lookups are wasteful.
SERVICE_IDS_TTL_SECONDS = 10 * 60


class ETAPredictor:
    def __init__(self) -> None:
        settings = get_settings()
        self._model_path = Path(settings.model_artifact_path)
        self._model_mtime: float | None = None
        self._ml_model: ResidualModel | None = None
        self._service_tz = settings.gtfs_service_tzinfo
        self._service_ids_cache: dict[tuple[str | None, str], tuple[float, set[str] | None]] = {}
        self._route_windows_cache: dict[tuple[str, str], tuple[float, dict[str, tuple[int, int]]]] = {}
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

        local_now = now.astimezone(self._service_tz)
        now_seconds = local_now.hour * 3600 + local_now.minute * 60 + local_now.second
        latest_updates = self._latest_trip_updates(db, stop_id, now, stop.feed_name)
        latest_vehicles = self._latest_vehicles(db, now, stop.feed_name)
        live_ids = set(latest_updates) | set(latest_vehicles)
        schedule_rows: list[tuple[StopTime, Trip, Route | None, datetime, bool]] = []
        # 24:xx belongs to the preceding service day, including its calendar.
        for offset in (0, 1):
            reference = local_now - timedelta(days=offset)
            valid_sids = self._valid_service_ids(db, reference, stop.feed_name)
            day_query = query
            if valid_sids is not None:
                day_query = day_query.where(Trip.service_id.in_(valid_sids))
            if offset:
                day_query = day_query.where(StopTime.arrival_seconds >= 86400)
            day_seconds = now_seconds + offset * 86400
            rows = db.execute(day_query.where(StopTime.arrival_seconds >= day_seconds).limit(25)).all()
            if live_ids:
                live_rows = db.execute(day_query.where(Trip.trip_id.in_(live_ids))).all()
                seen = {st.id for st, _, _ in rows}
                rows.extend(row for row in live_rows if row[0].id not in seen)
            if not rows:
                continue
            key = (stop.feed_name, reference.strftime("%Y%m%d"))
            cached = self._route_windows_cache.get(key)
            if cached is None or monotonic() - cached[0] >= SERVICE_IDS_TTL_SECONDS:
                if len(self._route_windows_cache) >= 16:
                    self._route_windows_cache.clear()
                cached = (monotonic(), route_windows(db, stop.feed_name, valid_sids))
                self._route_windows_cache[key] = cached
            operating = {rid for rid, (first, last) in cached[1].items() if first <= day_seconds <= last}
            schedule_rows.extend((st, trip, route, reference, trip.route_id in operating) for st, trip, route in rows)
        # Only trips with a live bus use their full schedule (delay from progress).
        trip_schedule_seconds = self._trip_schedule_seconds(
            db, [trip.id for _, trip, _, _, operating in schedule_rows if operating and trip.trip_id in latest_vehicles]
        )

        # A recurring trip_id can exist on both sides of midnight. Associate
        # realtime evidence with one service day, rather than showing one bus twice.
        live_days: dict[tuple[int, int | None], tuple[float, date]] = {}
        for st, trip, _, reference, operating in schedule_rows:
            if not operating or trip.trip_id not in live_ids:
                continue
            for vehicle in latest_vehicles.get(trip.trip_id, [None]):
                delay = delay_from_vehicle_progress(
                    now, vehicle, trip_schedule_seconds.get(trip.id, {}), reference, self._service_tz,
                ) if vehicle else None
                scheduled = scheduled_seconds_to_datetime(reference, st.arrival_seconds, self._service_tz)
                if scheduled is None:
                    continue
                update = latest_updates.get(trip.trip_id)
                target = datetime.fromtimestamp(update.arrival_time_unix, timezone.utc) if update and update.arrival_time_unix else now
                score = abs(delay) if delay is not None else abs((scheduled - target).total_seconds())
                key = (trip.id, vehicle.id if vehicle else None)
                if key not in live_days or score < live_days[key][0]:
                    live_days[key] = (score, reference.date())

        # Load complete paths once so both views apply the same ordering constraint.
        patterns: dict[int, list[tuple[StopTime, Stop]]] = {}
        if trip_schedule_seconds:
            for st, path_stop in db.execute(
                select(StopTime, Stop).join(Stop, Stop.id == StopTime.stop_id_ref)
                .where(StopTime.trip_id_ref.in_(trip_schedule_seconds))
                .order_by(StopTime.stop_sequence)
            ):
                patterns.setdefault(st.trip_id_ref, []).append((st, path_stop))
        paths: dict[tuple[int, int, date], dict[int, EtaItem]] = {}
        items: list[EtaItem] = []
        for stop_time, trip, route, reference, operating in schedule_rows:
            candidates = latest_vehicles.get(trip.trip_id, [None])
            has_live_vehicle = False
            for vehicle in candidates:
                key = (trip.id, vehicle.id if vehicle else None)
                has_live = operating and key in live_days and live_days[key][1] == reference.date()
                if vehicle is not None:
                    if not has_live:
                        continue
                    has_live_vehicle = True
                    path_key = (trip.id, vehicle.id, reference.date())
                    if path_key not in paths:
                        paths[path_key] = self.predict_vehicle_arrivals(
                            db, now=now, pattern=patterns.get(trip.id, []), trip=trip,
                            route=route, vehicle=vehicle, reference=reference, ml_model=ml_model,
                        )
                    item = paths[path_key].get(stop_time.stop_sequence)
                else:
                    item = self.predict_arrival(
                        now=now, stop=stop, stop_time=stop_time, trip=trip, route=route,
                        vehicle=None, trip_update=latest_updates.get(trip.trip_id) if has_live else None,
                        reference=reference, trip_schedule_seconds={}, ml_model=ml_model,
                    )
                if item is not None:
                    items.append(item)
            if candidates[0] is not None and not has_live_vehicle:
                item = self.predict_arrival(
                    now=now, stop=stop, stop_time=stop_time, trip=trip, route=route,
                    vehicle=None, trip_update=None, reference=reference,
                    trip_schedule_seconds={}, ml_model=ml_model,
                )
                if item is not None:
                    items.append(item)

        return sorted(items, key=lambda item: item.predicted_arrival_time)[:10]

    def predict_vehicle_arrivals(
        self, db: Session, *, now: datetime, pattern: list[tuple[StopTime, Stop]],
        trip: Trip, route: Route | None, vehicle: VehicleState, reference: datetime,
        ml_model: ResidualModel | None,
    ) -> dict[int, EtaItem]:
        """Predict one bus's complete path, identically for stop and journey views.

        Residuals are independent per stop. A later stop cannot arrive before an
        earlier one; carry the preceding estimate forward when necessary.
        Provider updates with a vehicle/sequence must match this exact bus/visit.
        """
        updates = list(db.execute(select(TripUpdateState).where(
            TripUpdateState.trip_id == trip.trip_id,
            or_(TripUpdateState.feed_name == trip.feed_name, TripUpdateState.feed_name.is_(None)),
            or_(TripUpdateState.vehicle_id == vehicle.vehicle_id, TripUpdateState.vehicle_id.is_(None)),
            TripUpdateState.observed_at >= now - timedelta(seconds=TRIP_UPDATE_MAX_AGE_SECONDS),
            TripUpdateState.observed_at <= now + timedelta(seconds=30),
        ).order_by(desc(TripUpdateState.observed_at))).scalars())
        schedule = {st.stop_sequence: st.arrival_seconds for st, _ in pattern}
        result: dict[int, EtaItem] = {}
        previous = None
        for st, stop in pattern:
            if vehicle.current_stop_sequence is None or st.stop_sequence < vehicle.current_stop_sequence:
                continue
            update = next((u for u in updates if u.stop_id == stop.stop_id
                           and (u.stop_sequence is None or u.stop_sequence == st.stop_sequence)), None)
            item = self.predict_arrival(
                now=now, stop=stop, stop_time=st, trip=trip, route=route,
                vehicle=vehicle, trip_update=update, reference=reference,
                trip_schedule_seconds=schedule, ml_model=ml_model,
            )
            if item is None:
                continue
            if previous is not None and item.predicted_arrival_time < previous:
                item = item.model_copy(update={
                    "predicted_arrival_time": previous,
                    "minutes_away": max(0, ceil((previous - now).total_seconds() / 60)),
                })
            previous = item.predicted_arrival_time
            result[st.stop_sequence] = item
        return result

    def predict_arrival(
        self, *, now: datetime, stop: Stop, stop_time: StopTime, trip: Trip,
        route: Route | None, vehicle: VehicleState | None, trip_update: TripUpdateState | None,
        reference: datetime, trip_schedule_seconds: dict[int, int | None],
        ml_model: ResidualModel | None,
    ) -> EtaItem | None:
        """One arrival calculation shared by stop cards and the live journey list."""
        scheduled_arrival = scheduled_seconds_to_datetime(reference, stop_time.arrival_seconds, self._service_tz)
        if (
            vehicle is not None
            and vehicle.current_stop_sequence is not None
            and vehicle.current_stop_sequence > stop_time.stop_sequence
        ):
            return None  # this bus has already passed the stop
        if vehicle is not None and not (trip_update and trip_update.arrival_time_unix):
            previous = None
            for sequence, seconds in sorted(trip_schedule_seconds.items()):
                if vehicle.current_stop_sequence is None or not vehicle.current_stop_sequence <= sequence <= stop_time.stop_sequence:
                    continue
                if seconds is None or (previous is not None and seconds < previous):
                    return None
                previous = seconds
        delay_seconds = trip_update.delay_seconds if trip_update and trip_update.delay_seconds is not None else None

        if delay_seconds is None and vehicle:
            delay_seconds = delay_from_vehicle_progress(
                now=now,
                vehicle=vehicle,
                trip_schedule_seconds=trip_schedule_seconds,
                service_reference=reference,
                service_tz=self._service_tz,
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
            and reference.astimezone(self._service_tz).date() == now.astimezone(self._service_tz).date()
            and delay_seconds is not None
            and (vehicle is None or vehicle.position_source != "kiosk")
            and predicted_arrival >= now
            and not (trip_update and trip_update.arrival_time_unix)
        ):
            evidence_time = vehicle.observed_at if vehicle else trip_update.observed_at
            if evidence_time.tzinfo is None:
                evidence_time = evidence_time.replace(tzinfo=timezone.utc)
            feature_row = build_feature_row(
                now=evidence_time,
                stop=stop,
                stop_time=stop_time,
                trip=trip,
                vehicle=vehicle,
                delay_seconds=delay_seconds,
            )
            if feature_row.baseline_eta_seconds is not None:
                residual_seconds = ml_model.predict(feature_row.features)
                adjusted_seconds = max(0, int(feature_row.baseline_eta_seconds + residual_seconds))
                predicted_arrival = evidence_time + timedelta(seconds=adjusted_seconds)
                source = "ml_residual"

        # Never clamp an expired prediction to a fictitious "Now" arrival.
        if predicted_arrival is None or predicted_arrival < now:
            return None

        minutes_away = None
        if predicted_arrival:
            minutes_away = max(0, ceil((predicted_arrival - now).total_seconds() / 60))

        data_timestamp = now
        if trip_update:
            data_timestamp = trip_update.observed_at
        elif vehicle:
            data_timestamp = (
                datetime.fromtimestamp(vehicle.timestamp_unix, timezone.utc)
                if vehicle.timestamp_unix is not None else vehicle.observed_at
            )

        return EtaItem(
            stop_id=stop.stop_id,
            stop_sequence=stop_time.stop_sequence,
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
            position_source=vehicle.position_source if vehicle else ("gtfs" if trip_update else None),
            data_timestamp=data_timestamp,
            delay_seconds=delay_seconds,
        )

    def list_route_vehicles(self, db: Session, route_id: str) -> list[VehicleState]:
        query = (
            select(VehicleState)
            .where(VehicleState.route_id == route_id)
            .order_by(desc(VehicleState.observed_at))
            .limit(100)
        )
        return list(db.execute(query).scalars().all())

    def _valid_service_ids(self, db: Session, now: datetime, feed_name: str | None = None) -> set[str] | None:
        local = now.astimezone(self._service_tz)
        date = local.strftime("%Y%m%d")
        key = (feed_name, date)
        cached = self._service_ids_cache.get(key)
        if cached is not None and monotonic() - cached[0] < SERVICE_IDS_TTL_SECONDS:
            return cached[1]
        ids = self._load_valid_service_ids(db, date, feed_name)
        if len(self._service_ids_cache) >= 16:
            self._service_ids_cache.clear()
        self._service_ids_cache[key] = (monotonic(), ids)
        return ids

    @staticmethod
    def _load_valid_service_ids(db: Session, date: str, feed_name: str | None = None) -> set[str] | None:
        return valid_service_ids(db, datetime.strptime(date, "%Y%m%d"), feed_name)

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
    def _latest_trip_updates(db: Session, stop_id: str, now: datetime, feed_name: str) -> dict[str, TripUpdateState]:
        rows = (
            db.execute(
                select(TripUpdateState)
                .where(
                    TripUpdateState.stop_id == stop_id,
                    or_(TripUpdateState.feed_name == feed_name, TripUpdateState.feed_name.is_(None)),
                    TripUpdateState.observed_at <= now + timedelta(seconds=30),
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
    def _latest_vehicles(db: Session, now: datetime, feed_name: str) -> dict[str, list[VehicleState]]:
        """Rank before matching trips, so old trips/stop matches cannot reappear."""
        rows = db.execute(
            select(VehicleState)
            .where(
                VehicleState.id.in_(latest_position_ids(now, VEHICLE_MAX_AGE_SECONDS)),
                or_(VehicleState.feed_name == feed_name, VehicleState.feed_name.is_(None)),
                VehicleState.current_stop_sequence.is_not(None),
            )
            .order_by(desc(VehicleState.timestamp_unix), desc(VehicleState.observed_at))
        ).scalars().all()
        result: dict[str, list[VehicleState]] = {}
        for row in rows:
            if row.trip_id:
                result.setdefault(row.trip_id, []).append(row)
        return result

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


@lru_cache(maxsize=1)
def get_predictor() -> ETAPredictor:
    """Share the loaded model between stop and journey requests."""
    return ETAPredictor()
