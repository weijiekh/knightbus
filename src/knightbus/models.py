from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from knightbus.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Route(Base):
    __tablename__ = "routes"

    id: Mapped[int] = mapped_column(primary_key=True)
    route_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    short_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    long_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    route_type: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Stop(Base):
    __tablename__ = "stops"

    id: Mapped[int] = mapped_column(primary_key=True)
    stop_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
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


class RawSnapshot(Base):
    __tablename__ = "raw_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_type: Mapped[str] = mapped_column(String(32), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[bytes] = mapped_column(nullable=False)


class VehicleState(Base):
    __tablename__ = "vehicle_states"
    __table_args__ = (
        Index("ix_vehicle_states_lookup", "stop_id", "trip_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[str] = mapped_column(String(128), index=True)
    trip_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    route_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    stop_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
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
    )

    id: Mapped[int] = mapped_column(primary_key=True)
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

