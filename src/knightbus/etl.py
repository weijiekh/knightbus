from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from knightbus.models import AggregatedSegmentStat, StopTime, Trip, TripUpdateState


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

    def scheduled_stop_count(self, db: Session) -> int:
        return int(db.execute(select(func.count()).select_from(StopTime)).scalar_one())

    def known_trip_count(self, db: Session) -> int:
        return int(db.execute(select(func.count()).select_from(Trip)).scalar_one())

