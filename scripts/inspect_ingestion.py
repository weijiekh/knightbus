from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from knightbus.db import Base, SessionLocal, engine
from knightbus.models import RawSnapshot, TripUpdateState, VehicleState


def format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.isoformat()


def print_row(label: str, count: int, latest_timestamp: datetime | None) -> None:
    print(f"{label:<24} count={count:<8} latest={format_timestamp(latest_timestamp)}")


def main() -> None:
    try:
        Base.metadata.create_all(bind=engine)

        with SessionLocal() as db:
            snapshot_rows: Iterable[tuple[str, int, datetime | None]] = db.execute(
                select(
                    RawSnapshot.feed_type,
                    func.count(RawSnapshot.id),
                    func.max(RawSnapshot.fetched_at),
                )
                .group_by(RawSnapshot.feed_type)
                .order_by(RawSnapshot.feed_type.asc())
            ).all()

            vehicle_count, latest_vehicle = db.execute(
                select(func.count(VehicleState.id), func.max(VehicleState.observed_at))
            ).one()
            trip_count, latest_trip = db.execute(
                select(func.count(TripUpdateState.id), func.max(TripUpdateState.observed_at))
            ).one()
    except OperationalError as exc:
        print("Database connection failed.")
        print("If you are running on the host machine, set DATABASE_URL to localhost instead of db.")
        print(f"Error: {exc}")
        return

    print("Raw snapshots by feed type")
    if not snapshot_rows:
        print("  no snapshots found")
    else:
        for feed_type, count, latest_timestamp in snapshot_rows:
            print_row(feed_type, int(count), latest_timestamp)

    print()
    print("Normalized tables")
    print_row("vehicle_states", int(vehicle_count), latest_vehicle)
    print_row("trip_update_states", int(trip_count), latest_trip)


if __name__ == "__main__":
    main()
