import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from knightbus.config import get_settings
from knightbus.features import CATEGORICAL_COLUMNS, FEATURE_COLUMNS, build_feature_row
from knightbus.ml import save_residual_model
from knightbus.models import ModelVersion, Stop, StopTime, Trip, VehicleState
from knightbus.utils import delay_from_vehicle_progress, haversine_meters

MIN_TRAINING_ROWS = 500
MAX_TRAINING_ROWS = 50000
ARRIVAL_RADIUS_METERS = 80.0
MIN_ARRIVAL_GAP_SECONDS = 120


@dataclass(frozen=True)
class TrainingRow:
    features: dict[str, object]
    baseline_eta_seconds: int
    label_eta_seconds: int
    observed_at: datetime


class TrainingService:
    def run_training(self, db: Session) -> ModelVersion:
        settings = get_settings()
        version = datetime.now(timezone.utc).strftime("lgbm-%Y%m%d%H%M%S")

        try:
            import lightgbm as lgb
            import numpy as np
            import pandas as pd
        except ImportError as exc:
            model = self._record_status(
                db,
                version=version,
                status="ml_unavailable",
                metrics={
                    "message": "ML dependencies are not installed. Install with `uv sync --extra ml`.",
                },
            )
            return model

        rows = self._collect_training_rows(db)
        if len(rows) < MIN_TRAINING_ROWS:
            model = self._record_status(
                db,
                version=version,
                status="insufficient_data",
                metrics={
                    "message": "Not enough labeled rows to train.",
                    "row_count": len(rows),
                },
            )
            return model

        rows.sort(key=lambda row: row.observed_at)
        df = pd.DataFrame([row.features for row in rows])
        df["baseline_eta_seconds"] = [row.baseline_eta_seconds for row in rows]
        df["label_eta_seconds"] = [row.label_eta_seconds for row in rows]
        df["observed_at"] = [row.observed_at for row in rows]

        n = len(df)
        test_size = max(1, int(n * 0.15))
        val_size = max(1, int(n * 0.15))
        train_size = n - test_size - val_size
        if train_size < 200:
            model = self._record_status(
                db,
                version=version,
                status="insufficient_data",
                metrics={
                    "message": "Not enough rows after split.",
                    "row_count": len(rows),
                },
            )
            return model

        train_df = df.iloc[:train_size].copy()
        val_df = df.iloc[train_size : train_size + val_size].copy()
        test_df = df.iloc[train_size + val_size :].copy()

        for col in CATEGORICAL_COLUMNS:
            for split in (train_df, val_df, test_df):
                if col in split.columns:
                    split[col] = split[col].astype("category")

        X_train = train_df[FEATURE_COLUMNS]
        y_train = train_df["label_eta_seconds"] - train_df["baseline_eta_seconds"]

        X_val = val_df[FEATURE_COLUMNS]
        y_val = val_df["label_eta_seconds"] - val_df["baseline_eta_seconds"]

        model = lgb.LGBMRegressor(
            n_estimators=300,
            learning_rate=0.05,
            num_leaves=64,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
        )
        model.fit(
            X_train,
            y_train,
            categorical_feature=[col for col in CATEGORICAL_COLUMNS if col in X_train.columns],
            eval_set=[(X_val, y_val)],
        )

        def evaluate(split_df: pd.DataFrame) -> dict[str, float]:
            X_split = split_df[FEATURE_COLUMNS]
            residual_pred = model.predict(X_split)
            eta_pred = split_df["baseline_eta_seconds"].to_numpy() + residual_pred
            eta_true = split_df["label_eta_seconds"].to_numpy()
            abs_err = np.abs(eta_pred - eta_true)
            return {
                "mae": float(np.mean(abs_err)),
                "p50": float(np.percentile(abs_err, 50)),
                "p90": float(np.percentile(abs_err, 90)),
            }

        def baseline_metrics(split_df: pd.DataFrame) -> dict[str, float]:
            eta_pred = split_df["baseline_eta_seconds"].to_numpy()
            eta_true = split_df["label_eta_seconds"].to_numpy()
            abs_err = np.abs(eta_pred - eta_true)
            return {
                "mae": float(np.mean(abs_err)),
                "p50": float(np.percentile(abs_err, 50)),
                "p90": float(np.percentile(abs_err, 90)),
            }

        metrics = {
            "rows": len(rows),
            "train_rows": len(train_df),
            "val_rows": len(val_df),
            "test_rows": len(test_df),
            "baseline": {
                "val": baseline_metrics(val_df),
                "test": baseline_metrics(test_df),
            },
            "model": {
                "val": evaluate(val_df),
                "test": evaluate(test_df),
            },
            "feature_columns": FEATURE_COLUMNS,
            "categorical_columns": CATEGORICAL_COLUMNS,
            "artifact_path": settings.model_artifact_path,
        }

        save_residual_model(
            settings.model_artifact_path,
            model=model,
            feature_columns=FEATURE_COLUMNS,
            categorical_columns=CATEGORICAL_COLUMNS,
            metrics=metrics,
        )

        model_record = self._record_status(
            db,
            version=version,
            status="trained",
            metrics=metrics,
        )
        return model_record

    def _collect_training_rows(self, db: Session) -> list[TrainingRow]:
        rows: list[TrainingRow] = []
        schedule_cache: dict[int, dict[int, int | None]] = {}
        stop_time_cache: dict[tuple[int, int], StopTime | None] = {}
        stop_cache: dict[int, Stop | None] = {}
        trip_stops_cache: dict[int, list[tuple[int, Stop, StopTime]]] = {}

        query = (
            select(VehicleState, Trip)
            .join(Trip, VehicleState.trip_id == Trip.trip_id)
            .where(
                VehicleState.trip_id.is_not(None),
                VehicleState.lat.is_not(None),
                VehicleState.lon.is_not(None),
            )
            .order_by(desc(VehicleState.observed_at))
            .limit(MAX_TRAINING_ROWS)
        )

        by_trip: dict[str, list[VehicleState]] = {}
        trip_lookup: dict[str, Trip] = {}
        for vehicle, trip in db.execute(query).all():
            by_trip.setdefault(vehicle.trip_id, []).append(vehicle)
            trip_lookup[vehicle.trip_id] = trip

        for trip_id, states in by_trip.items():
            trip = trip_lookup[trip_id]
            states.sort(key=lambda item: item.observed_at)
            trip_stops = self._get_trip_stops(db, trip_stops_cache, stop_cache, trip.id)
            if not trip_stops:
                continue

            arrival_events = self._derive_arrival_events(states, trip_stops)
            if not arrival_events:
                continue

            event_index = 0
            for vehicle in states:
                while event_index < len(arrival_events) and arrival_events[event_index][0] < vehicle.observed_at:
                    event_index += 1
                if event_index >= len(arrival_events):
                    break

                arrival_time, stop_time, stop = arrival_events[event_index]
                label_eta_seconds = int((arrival_time - vehicle.observed_at).total_seconds())
                if label_eta_seconds <= 0 or label_eta_seconds > 4 * 3600:
                    continue

                trip_schedule_seconds = self._get_trip_schedule(db, schedule_cache, trip.id)
                delay_seconds = delay_from_vehicle_progress(
                    now=vehicle.observed_at,
                    vehicle=vehicle,
                    trip_schedule_seconds=trip_schedule_seconds,
                )

                feature_row = build_feature_row(
                    now=vehicle.observed_at,
                    stop=stop,
                    stop_time=stop_time,
                    trip=trip,
                    vehicle=vehicle,
                    delay_seconds=delay_seconds,
                )
                if feature_row.baseline_eta_seconds is None:
                    continue

                rows.append(
                    TrainingRow(
                        features=feature_row.features,
                        baseline_eta_seconds=feature_row.baseline_eta_seconds,
                        label_eta_seconds=label_eta_seconds,
                        observed_at=vehicle.observed_at,
                    )
                )

        return rows

    @staticmethod
    def _get_trip_schedule(
        db: Session,
        cache: dict[int, dict[int, int | None]],
        trip_id_ref: int,
    ) -> dict[int, int | None]:
        if trip_id_ref in cache:
            return cache[trip_id_ref]
        rows = (
            db.execute(
                select(StopTime.stop_sequence, StopTime.arrival_seconds).where(StopTime.trip_id_ref == trip_id_ref)
            )
            .all()
        )
        schedule = {sequence: arrival_seconds for sequence, arrival_seconds in rows}
        cache[trip_id_ref] = schedule
        return schedule

    @staticmethod
    def _get_stop_time(
        db: Session,
        cache: dict[tuple[int, int], StopTime | None],
        trip_id_ref: int,
        stop_sequence: int,
    ) -> StopTime | None:
        key = (trip_id_ref, stop_sequence)
        if key in cache:
            return cache[key]
        stop_time = (
            db.execute(
                select(StopTime).where(
                    StopTime.trip_id_ref == trip_id_ref,
                    StopTime.stop_sequence == stop_sequence,
                )
            )
            .scalars()
            .first()
        )
        cache[key] = stop_time
        return stop_time

    @staticmethod
    def _get_stop(db: Session, cache: dict[int, Stop | None], stop_id_ref: int) -> Stop | None:
        if stop_id_ref in cache:
            return cache[stop_id_ref]
        stop = db.execute(select(Stop).where(Stop.id == stop_id_ref)).scalars().first()
        cache[stop_id_ref] = stop
        return stop

    @staticmethod
    def _get_trip_stops(
        db: Session,
        trip_stops_cache: dict[int, list[tuple[int, Stop, StopTime]]],
        stop_cache: dict[int, Stop | None],
        trip_id_ref: int,
    ) -> list[tuple[int, Stop, StopTime]]:
        if trip_id_ref in trip_stops_cache:
            return trip_stops_cache[trip_id_ref]
        stop_times = (
            db.execute(
                select(StopTime)
                .where(StopTime.trip_id_ref == trip_id_ref)
                .order_by(StopTime.stop_sequence.asc())
            )
            .scalars()
            .all()
        )
        trip_stops: list[tuple[int, Stop, StopTime]] = []
        for stop_time in stop_times:
            stop = TrainingService._get_stop(db, stop_cache, stop_time.stop_id_ref)
            if stop is None or stop.lat is None or stop.lon is None:
                continue
            trip_stops.append((stop_time.stop_sequence, stop, stop_time))
        trip_stops_cache[trip_id_ref] = trip_stops
        return trip_stops

    @staticmethod
    def _derive_arrival_events(
        states: list[VehicleState],
        trip_stops: list[tuple[int, Stop, StopTime]],
    ) -> list[tuple[datetime, StopTime, Stop]]:
        events: list[tuple[datetime, StopTime, Stop]] = []
        last_seen: dict[int, datetime] = {}
        for state in states:
            if state.lat is None or state.lon is None:
                continue
            closest: tuple[float, Stop, StopTime] | None = None
            for _, stop, stop_time in trip_stops:
                distance = haversine_meters(state.lat, state.lon, stop.lat, stop.lon)
                if closest is None or distance < closest[0]:
                    closest = (distance, stop, stop_time)
            if closest is None:
                continue
            distance, stop, stop_time = closest
            if distance > ARRIVAL_RADIUS_METERS:
                continue
            last_time = last_seen.get(stop_time.stop_sequence)
            if last_time and (state.observed_at - last_time).total_seconds() < MIN_ARRIVAL_GAP_SECONDS:
                continue
            last_seen[stop_time.stop_sequence] = state.observed_at
            events.append((state.observed_at, stop_time, stop))
        return events

    @staticmethod
    def _record_status(db: Session, version: str, status: str, metrics: dict[str, object]) -> ModelVersion:
        model = ModelVersion(
            version=version,
            status=status,
            metrics_json=json.dumps(metrics),
        )
        db.add(model)
        db.commit()
        db.refresh(model)
        return model


def main() -> None:
    from knightbus.db import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        model = TrainingService().run_training(db)
        print(model.version)
