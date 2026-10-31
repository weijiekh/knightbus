from functools import lru_cache
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FeedConfig(BaseModel):
    name: str
    static_url: str | None = None
    vehicle_positions_url: str | None = None
    trip_updates_url: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    database_url: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/knightbus",
        alias="DATABASE_URL",
    )
    gtfs_feeds: list[FeedConfig] = Field(default_factory=list, alias="GTFS_FEEDS")
    gtfs_static_url: str | None = Field(default=None, alias="GTFS_STATIC_URL")
    gtfs_vehicle_positions_url: str | None = Field(default=None, alias="GTFS_VEHICLE_POSITIONS_URL")
    gtfs_trip_updates_url: str | None = Field(default=None, alias="GTFS_TRIP_UPDATES_URL")
    gtfs_request_timeout_seconds: int = Field(default=20, alias="GTFS_REQUEST_TIMEOUT_SECONDS")
    gtfs_poll_interval_seconds: int = Field(default=30, alias="GTFS_POLL_INTERVAL_SECONDS")
    gtfs_service_timezone: str = Field(default="Asia/Kuala_Lumpur", alias="GTFS_SERVICE_TIMEZONE")
    gtfs_active_poll_start_local: str = Field(default="06:00", alias="GTFS_ACTIVE_POLL_START_LOCAL")
    gtfs_active_poll_end_local: str = Field(default="00:00", alias="GTFS_ACTIVE_POLL_END_LOCAL")
    gtfs_off_hours_poll_interval_seconds: int = Field(default=900, alias="GTFS_OFF_HOURS_POLL_INTERVAL_SECONDS")
    gtfs_static_refresh_hours: int = Field(default=24, alias="GTFS_STATIC_REFRESH_HOURS")
    feature_refresh_minutes: int = Field(default=5, alias="FEATURE_REFRESH_MINUTES")
    training_refresh_hours: int = Field(default=24, alias="TRAINING_REFRESH_HOURS")
    training_in_worker: bool = Field(default=True, alias="TRAINING_IN_WORKER")
    model_artifact_path: str = Field(default="artifacts/eta_lgbm.pkl", alias="MODEL_ARTIFACT_PATH")
    realtime_retention_days: int = Field(default=14, alias="REALTIME_RETENTION_DAYS")
    persist_raw_snapshots: bool = Field(default=False, alias="PERSIST_RAW_SNAPSHOTS")
    raw_snapshot_retention_days: int = Field(default=1, alias="RAW_SNAPSHOT_RETENTION_DAYS")

    @field_validator("gtfs_service_timezone")
    @classmethod
    def validate_gtfs_service_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"invalid timezone: {value}") from exc
        return value

    @field_validator("gtfs_active_poll_start_local", "gtfs_active_poll_end_local")
    @classmethod
    def validate_local_time(cls, value: str) -> str:
        cls.parse_local_time(value)
        return value

    @field_validator("gtfs_off_hours_poll_interval_seconds")
    @classmethod
    def validate_gtfs_off_hours_poll_interval_seconds(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("GTFS_OFF_HOURS_POLL_INTERVAL_SECONDS must be greater than 0")
        return value

    @staticmethod
    def parse_local_time(value: str) -> time:
        try:
            return time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"invalid local time '{value}', expected HH:MM") from exc

    @property
    def gtfs_service_tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.gtfs_service_timezone)

    @property
    def gtfs_active_poll_start_time(self) -> time:
        return self.parse_local_time(self.gtfs_active_poll_start_local)

    @property
    def gtfs_active_poll_end_time(self) -> time:
        return self.parse_local_time(self.gtfs_active_poll_end_local)

    @property
    def resolved_feeds(self) -> list[FeedConfig]:
        if self.gtfs_feeds:
            return self.gtfs_feeds
        return [
            FeedConfig(
                name="default",
                static_url=self.gtfs_static_url,
                vehicle_positions_url=self.gtfs_vehicle_positions_url,
                trip_updates_url=self.gtfs_trip_updates_url,
            )
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
