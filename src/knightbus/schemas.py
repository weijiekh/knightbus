from datetime import datetime

from pydantic import BaseModel


class EtaItem(BaseModel):
    stop_id: str
    route_id: str | None = None
    trip_id: str | None = None
    headsign: str | None = None
    vehicle_id: str | None = None
    predicted_arrival_time: datetime | None = None
    scheduled_arrival_time: datetime | None = None
    minutes_away: int | None = None
    prediction_source: str
    data_timestamp: datetime
    delay_seconds: int | None = None


class StopEtaResponse(BaseModel):
    stop_id: str
    generated_at: datetime
    items: list[EtaItem]


class VehicleResponse(BaseModel):
    vehicle_id: str
    route_id: str | None = None
    trip_id: str | None = None
    stop_id: str | None = None
    current_stop_sequence: int | None = None
    lat: float | None = None
    lon: float | None = None
    observed_at: datetime


class StopResponse(BaseModel):
    stop_id: str
    name: str | None = None
    lat: float | None = None
    lon: float | None = None
    distance_m: float | None = None


class HealthResponse(BaseModel):
    status: str
    environment: str


class ModelInfoResponse(BaseModel):
    version: str | None = None
    status: str
    metrics_json: str | None = None

