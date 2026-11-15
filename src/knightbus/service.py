"""GTFS service-day calendars and route operating spans."""
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from knightbus.models import CalendarDate, ServiceCalendar, StopTime, Trip


def valid_service_ids(db: Session, day: datetime, feed_name: str | None = None) -> set[str] | None:
    date = day.strftime("%Y%m%d")
    calendars = select(ServiceCalendar)
    exceptions = select(CalendarDate)
    if feed_name is not None:
        calendars = calendars.where(ServiceCalendar.feed_name == feed_name)
        exceptions = exceptions.where(CalendarDate.feed_name == feed_name)
    has_calendar = db.execute(calendars.limit(1)).first() is not None
    has_exceptions = db.execute(exceptions.limit(1)).first() is not None
    if not has_calendar and not has_exceptions:
        return None
    weekday = getattr(ServiceCalendar, day.strftime("%A").lower())
    rows = db.execute(calendars.where(
        weekday.is_(True), ServiceCalendar.start_date <= date, ServiceCalendar.end_date >= date,
    )).scalars()
    valid = {row.service_id for row in rows}
    for row in db.execute(exceptions.where(CalendarDate.date == date)).scalars():
        if row.exception_type == 1:
            valid.add(row.service_id)
        elif row.exception_type == 2:
            valid.discard(row.service_id)
    return valid


def route_windows(db: Session, feed_name: str, service_ids: set[str] | None,
                  route_id: str | None = None) -> dict[str, tuple[int, int]]:
    query = select(Trip.route_id, func.min(StopTime.arrival_seconds), func.max(StopTime.arrival_seconds)).join(
        StopTime, StopTime.trip_id_ref == Trip.id,
    ).where(Trip.feed_name == feed_name).group_by(Trip.route_id)
    if service_ids is not None:
        query = query.where(Trip.service_id.in_(service_ids))
    if route_id is not None:
        query = query.where(Trip.route_id == route_id)
    return {rid: (first, last) for rid, first, last in db.execute(query)
            if rid is not None and first is not None and last is not None}
