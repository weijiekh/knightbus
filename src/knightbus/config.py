from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    database_url: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/knightbus",
        alias="DATABASE_URL",
    )
    gtfs_static_url: str | None = Field(default=None, alias="GTFS_STATIC_URL")
    gtfs_vehicle_positions_url: str | None = Field(default=None, alias="GTFS_VEHICLE_POSITIONS_URL")
    gtfs_trip_updates_url: str | None = Field(default=None, alias="GTFS_TRIP_UPDATES_URL")
    gtfs_request_timeout_seconds: int = Field(default=20, alias="GTFS_REQUEST_TIMEOUT_SECONDS")
    gtfs_poll_interval_seconds: int = Field(default=30, alias="GTFS_POLL_INTERVAL_SECONDS")
    gtfs_static_refresh_hours: int = Field(default=24, alias="GTFS_STATIC_REFRESH_HOURS")
    feature_refresh_minutes: int = Field(default=5, alias="FEATURE_REFRESH_MINUTES")
    training_refresh_hours: int = Field(default=24, alias="TRAINING_REFRESH_HOURS")


@lru_cache
def get_settings() -> Settings:
    return Settings()

