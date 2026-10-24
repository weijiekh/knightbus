from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from knightbus.models import AggregatedSegmentStat, RawSnapshot, StopTime, Trip, TripUpdateState, VehicleState


class ETLService:
    def refresh_segment_statistics(self, db: Session) -> int:
        now_hour = datetime.now(timezone.utc).hour

        updates = (
            db.execute(
                select(
                    TripUpdateState.route_id,
                    TripUpdateState.stop_id,
                    TripUpdateState.delay_seconds,
                )
                .where(TripUpdateState.route_id.is_not(None), TripUpdateState.stop_id.is_not(None))
                .order_by(TripUpdateState.observed_at.desc())
                .limit(1000)
            )
            .all()
        )

        grouped: dict[tuple[str, str], list[int]] = {}
        for route_id, stop_id, delay_seconds in updates:
            if route_id is None or stop_id is None or delay_seconds is None:
                continue
            grouped.setdefault((route_id, stop_id), []).append(abs(delay_seconds))

        count = 0
        for (route_id, stop_id), values in grouped.items():
            median_value = int(sorted(values)[len(values) // 2])
            row = (
                db.execute(
                    select(AggregatedSegmentStat).where(
                        AggregatedSegmentStat.route_id == route_id,
                        AggregatedSegmentStat.hour_of_day == now_hour,
                        AggregatedSegmentStat.from_stop_id == stop_id,
                        AggregatedSegmentStat.to_stop_id == stop_id,
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                row = AggregatedSegmentStat(
                    route_id=route_id,
                    hour_of_day=now_hour,
                    from_stop_id=stop_id,
                    to_stop_id=stop_id,
                    median_travel_seconds=median_value,
                    sample_count=len(values),
                )
                db.add(row)
            else:
                row.median_travel_seconds = median_value
                row.sample_count = len(values)
            count += 1

        db.commit()
        return count

    def prune_realtime_states(
        self, db: Session, realtime_days: int, raw_days: int, now: datetime | None = None
    ) -> int:
        now = now or datetime.now(timezone.utc)
        targets = []
        if realtime_days > 0:
            cutoff = now - timedelta(days=realtime_days)
            targets += [
                (VehicleState, VehicleState.observed_at < cutoff),
                (TripUpdateState, TripUpdateState.observed_at < cutoff),
            ]
        if raw_days > 0:
            targets.append((RawSnapshot, RawSnapshot.fetched_at < now - timedelta(days=raw_days)))
        deleted = 0
        for model, condition in targets:
            deleted += db.execute(delete(model).where(condition).execution_options(synchronize_session=False)).rowcount or 0
        db.commit()
        return deleted

    def scheduled_stop_count(self, db: Session) -> int:
        return int(db.execute(select(func.count()).select_from(StopTime)).scalar_one())

    def known_trip_count(self, db: Session) -> int:
        return int(db.execute(select(func.count()).select_from(Trip)).scalar_one())

