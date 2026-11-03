from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from knightbus.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Route(Base):
    __tablename__ = "routes"
    __table_args__ = (
        UniqueConstraint("feed_name", "route_id", name="uq_routes_feed_route"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_name: Mapped[str] = mapped_column(String(64), default="default", index=True)
    route_id: Mapped[str] = mapped_column(String(64), index=True)
    short_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    long_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    route_type: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Stop(Base):
    __tablename__ = "stops"
    __table_args__ = (
        UniqueConstraint("feed_name", "stop_id", name="uq_stops_feed_stop"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_name: Mapped[str] = mapped_column(String(64), default="default", index=True)
    stop_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)


Index("ix_stops_lower_name", func.lower(Stop.name))
Index("ix_stops_lat_lon", Stop.lat, Stop.lon)  # nearby-stops bounding box


class Trip(Base):
    __tablename__ = "trips"
    __table_args__ = (
        UniqueConstraint("feed_name", "trip_id", name="uq_trips_feed_trip"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_name: Mapped[str] = mapped_column(String(64), default="default", index=True)
    trip_id: Mapped[str] = mapped_column(String(128), index=True)
    route_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    service_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    headsign: Mapped[str | None] = mapped_column(String(255), nullable=True)
    direction_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    stop_times: Mapped[list["StopTime"]] = relationship(back_populates="trip")


class StopTime(Base):
    __tablename__ = "stop_times"
    __table_args__ = (
        UniqueConstraint("trip_id_ref", "stop_sequence", name="uq_stop_times_trip_sequence"),
        Index("ix_stop_times_stop_lookup", "stop_id_ref", "arrival_seconds"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id_ref: Mapped[int] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"))
    stop_id_ref: Mapped[int] = mapped_column(ForeignKey("stops.id", ondelete="CASCADE"))
    stop_sequence: Mapped[int] = mapped_column(Integer)
    arrival_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    departure_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    arrival_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    departure_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    trip: Mapped[Trip] = relationship(back_populates="stop_times")
    stop: Mapped[Stop] = relationship()


class ServiceCalendar(Base):
    __tablename__ = "service_calendar"
    __table_args__ = (
        UniqueConstraint("feed_name", "service_id", name="uq_service_calendar_feed_service"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_name: Mapped[str] = mapped_column(String(64), default="default", index=True)
    service_id: Mapped[str] = mapped_column(String(64), index=True)
    monday: Mapped[bool] = mapped_column(Boolean, default=False)
    tuesday: Mapped[bool] = mapped_column(Boolean, default=False)
    wednesday: Mapped[bool] = mapped_column(Boolean, default=False)
    thursday: Mapped[bool] = mapped_column(Boolean, default=False)
    friday: Mapped[bool] = mapped_column(Boolean, default=False)
    saturday: Mapped[bool] = mapped_column(Boolean, default=False)
    sunday: Mapped[bool] = mapped_column(Boolean, default=False)
    start_date: Mapped[str] = mapped_column(String(8))  # YYYYMMDD
    end_date: Mapped[str] = mapped_column(String(8))    # YYYYMMDD


class CalendarDate(Base):
    __tablename__ = "calendar_dates"
    __table_args__ = (
        UniqueConstraint("feed_name", "service_id", "date", name="uq_calendar_dates_feed_service_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_name: Mapped[str] = mapped_column(String(64), default="default", index=True)
    service_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[str] = mapped_column(String(8), index=True)  # YYYYMMDD
    exception_type: Mapped[int] = mapped_column(Integer)  # 1=added, 2=removed


class StaticFeedState(Base):
    __tablename__ = "static_feed_state"

    feed_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64))
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RawSnapshot(Base):
    __tablename__ = "raw_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_type: Mapped[str] = mapped_column(String(32), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[bytes] = mapped_column(nullable=False)


class VehicleState(Base):
    __tablename__ = "vehicle_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Only trip_id, route_id and observed_at are queried; every extra index on
    # this hot insert table costs cache and write memory.
    feed_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vehicle_id: Mapped[str] = mapped_column(String(128))
    trip_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    route_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    stop_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_stop_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    timestamp_unix: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class TripUpdateState(Base):
    __tablename__ = "trip_update_states"
    __table_args__ = (
        Index("ix_trip_updates_lookup", "trip_id", "stop_id", "observed_at"),
        # Stop ETA lookup: newest updates for one stop.
        Index("ix_trip_updates_stop_recent", "stop_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    trip_id: Mapped[str] = mapped_column(String(128), index=True)
    route_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    vehicle_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    stop_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    stop_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    arrival_time_unix: Mapped[int | None] = mapped_column(Integer, nullable=True)
    departure_time_unix: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delay_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AggregatedSegmentStat(Base):
    __tablename__ = "aggregated_segment_stats"
    __table_args__ = (
        UniqueConstraint("route_id", "hour_of_day", "from_stop_id", "to_stop_id", name="uq_segment_stats_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    route_id: Mapped[str] = mapped_column(String(64), index=True)
    hour_of_day: Mapped[int] = mapped_column(Integer, index=True)
    from_stop_id: Mapped[str] = mapped_column(String(64), index=True)
    to_stop_id: Mapped[str] = mapped_column(String(64), index=True)
    median_travel_seconds: Mapped[int] = mapped_column(Integer)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ModelVersion(Base):
    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

