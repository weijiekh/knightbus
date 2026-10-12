import io
import zipfile
from datetime import datetime, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from knightbus.collector import FeedPayload, GTFSCollector
from knightbus.config import FeedConfig
from knightbus.db import Base
from knightbus.models import CalendarDate, Route, ServiceCalendar, Stop, StopTime, Trip

FILES = {
    "routes.txt": "route_id,route_short_name,route_long_name,route_type\nR1,1,Line One,3\nR2,2,Line Two,3\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,Alpha,3.1,101.6\nS2,Beta,3.2,101.7\nS3,Gamma,3.3,101.8\n",
    "trips.txt": "route_id,service_id,trip_id,trip_headsign,direction_id\nR1,WD,T1,North,0\nR2,WD,T2,South,1\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,08:00:00,08:00:30,S1,1\n"
        "T1,08:10:00,08:10:30,S2,2\n"
        "T2,25:00:00,25:00:00,S3,1\n"
        "T2,25:05:00,25:05:00,S-UNKNOWN,2\n"
        "T-UNKNOWN,09:00:00,09:00:00,S1,1\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "WD,1,1,1,1,1,0,0,20260101,20261231\n"
    ),
    "calendar_dates.txt": "service_id,date,exception_type\nWD,20261225,2\nWD,20261226,1\n",
}


def make_payload() -> FeedPayload:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, content in FILES.items():
            archive.writestr(name, content)
    return FeedPayload(url="http://x/static.zip", fetched_at=datetime(2026, 10, 12, tzinfo=timezone.utc), payload=buf.getvalue())


def build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def collector(name: str) -> GTFSCollector:
    return GTFSCollector(FeedConfig(name=name, static_url="http://x/static.zip"))


def count(db, model, feed_name: str) -> int:
    return db.scalar(select(func.count()).select_from(model).where(model.feed_name == feed_name))


def test_load_static_feed_rows_and_links() -> None:
    db = build_session()
    collector("kl").load_static_feed(db, make_payload())

    assert count(db, Route, "kl") == 2
    assert count(db, Stop, "kl") == 3
    assert count(db, Trip, "kl") == 2
    assert count(db, ServiceCalendar, "kl") == 1
    assert count(db, CalendarDate, "kl") == 2
    assert db.scalar(select(func.count()).select_from(StopTime)) == 3

    rows = db.execute(
        select(Trip.trip_id, Stop.stop_id, StopTime.stop_sequence, StopTime.arrival_seconds, StopTime.departure_seconds)
        .join(Trip, Trip.id == StopTime.trip_id_ref)
        .join(Stop, Stop.id == StopTime.stop_id_ref)
        .order_by(Trip.trip_id, StopTime.stop_sequence)
    ).all()
    assert rows == [
        ("T1", "S1", 1, 8 * 3600, 8 * 3600 + 30),
        ("T1", "S2", 2, 8 * 3600 + 600, 8 * 3600 + 630),
        ("T2", "S3", 1, 25 * 3600, 25 * 3600),
    ]
    cal = db.scalars(select(ServiceCalendar)).one()
    assert cal.monday and not cal.saturday
    assert sorted((d.date, d.exception_type) for d in db.scalars(select(CalendarDate))) == [("20261225", 2), ("20261226", 1)]


def test_reload_replaces_and_other_feed_untouched() -> None:
    db = build_session()
    collector("penang").load_static_feed(db, make_payload())
    collector("kl").load_static_feed(db, make_payload())
    collector("kl").load_static_feed(db, make_payload())

    assert count(db, Route, "kl") == 2
    assert count(db, Stop, "kl") == 3
    assert count(db, Trip, "kl") == 2
    assert count(db, Route, "penang") == 2
    assert count(db, Trip, "penang") == 2
    assert count(db, ServiceCalendar, "kl") == 1
    assert count(db, CalendarDate, "kl") == 2
    assert db.scalar(select(func.count()).select_from(StopTime)) == 6
