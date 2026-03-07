from datetime import datetime, timedelta, timezone


def parse_gtfs_time_to_seconds(value: str | None) -> int | None:
    if not value:
        return None

    parts = value.split(":")
    if len(parts) != 3:
        return None

    hours, minutes, seconds = [int(part) for part in parts]
    return hours * 3600 + minutes * 60 + seconds


def scheduled_seconds_to_datetime(reference: datetime, seconds: int | None) -> datetime | None:
    if seconds is None:
        return None

    day_start = reference.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start + timedelta(seconds=seconds)

