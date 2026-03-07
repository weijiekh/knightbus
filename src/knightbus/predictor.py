from datetime import datetime, timezone

from sqlalchemy import Select, desc, select
from sqlalchemy.orm import Session

from knightbus.models import ModelVersion, Stop, StopTime, Trip, TripUpdateState, VehicleState
from knightbus.schemas import EtaItem, ModelInfoResponse
from knightbus.utils import scheduled_seconds_to_datetime


class ETAPredictor:
    def get_stop_etas(self, db: Session, stop_id: str, route_id: str | None = None) -> list[EtaItem]:
        now = datetime.now(timezone.utc)
        stop = db.execute(select(Stop).where(Stop.stop_id == stop_id)).scalars().first()
        if stop is None:
            return []

        query: Select[tuple[StopTime, Trip]] = (
            select(StopTime, Trip)
            .join(Trip, StopTime.trip_id_ref == Trip.id)
            .where(StopTime.stop_id_ref == stop.id)
            .order_by(StopTime.arrival_seconds.asc())
        )
        if route_id:
            query = query.where(Trip.route_id == route_id)

        schedule_rows = db.execute(query.limit(25)).all()
        latest_updates = self._latest_trip_updates(db, stop_id)
        latest_vehicles = self._latest_vehicles(db, stop_id)
        trip_schedule_seconds = self._trip_schedule_seconds(db, [trip.id for _, trip in schedule_rows])

        items: list[EtaItem] = []
        for stop_time, trip in schedule_rows:
            scheduled_arrival = scheduled_seconds_to_datetime(now, stop_time.arrival_seconds)
            trip_update = latest_updates.get(trip.trip_id)
            vehicle = latest_vehicles.get(trip.trip_id)
            delay_seconds = trip_update.delay_seconds if trip_update and trip_update.delay_seconds is not None else None

            if delay_seconds is None and vehicle:
                delay_seconds = self._delay_from_vehicle_progress(
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

            minutes_away = None
            if predicted_arrival:
                minutes_away = max(0, int((predicted_arrival - now).total_seconds() // 60))

            items.append(
                EtaItem(
                    stop_id=stop_id,
                    route_id=trip.route_id,
                    trip_id=trip.trip_id,
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

    def current_model_info(self, db: Session) -> ModelInfoResponse:
        version = db.execute(select(ModelVersion).order_by(desc(ModelVersion.created_at)).limit(1)).scalars().first()
        if version is None:
            return ModelInfoResponse(status="not_trained")
        return ModelInfoResponse(version=version.version, status=version.status, metrics_json=version.metrics_json)

    @staticmethod
    def _latest_trip_updates(db: Session, stop_id: str) -> dict[str, TripUpdateState]:
        rows = (
            db.execute(
                select(TripUpdateState)
                .where(TripUpdateState.stop_id == stop_id)
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
    def _latest_vehicles(db: Session, stop_id: str) -> dict[str, VehicleState]:
        rows = (
            db.execute(
                select(VehicleState)
                .where(VehicleState.stop_id == stop_id, VehicleState.trip_id.is_not(None))
                .order_by(desc(VehicleState.observed_at))
                .limit(200)
            )
            .scalars()
            .all()
        )
        latest: dict[str, VehicleState] = {}
        for row in rows:
            if row.trip_id:
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

    @staticmethod
    def _delay_from_vehicle_progress(
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
