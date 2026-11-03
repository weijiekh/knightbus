from datetime import datetime

from pydantic import BaseModel


class EtaItem(BaseModel):
    stop_id: str
    route_id: str | None = None
    route_short_name: str | None = None
    route_long_name: str | None = None
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
    stop_name: str | None = None
    stop_code: str | None = None
    lat: float | None = None
    lon: float | None = None
    generated_at: datetime
    items: list[EtaItem]


class VehicleResponse(BaseModel):
    headsign: str | None = None
    direction_id: int | None = None
    stop_name: str | None = None
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



class SearchStop(BaseModel):
    stop_id: str
    code: str | None = None
    name: str | None = None
    lat: float | None = None
    lon: float | None = None
    distance_m: float | None = None
    routes: list[str] = []


class SearchRoute(BaseModel):
    route_id: str
    short_name: str | None = None
    long_name: str | None = None
    feed_name: str


class SearchResponse(BaseModel):
    query: str
    stops: list[SearchStop]
    routes: list[SearchRoute]


class WalkRouteResponse(BaseModel):
    distance_m: float
    duration_s: float
    geometry: list[list[float]]  # [[lat, lon], ...]


class TripStop(BaseModel):
    stop_id: str
    stop_sequence: int
    stop_code: str | None = None
    stop_name: str | None = None
    lat: float | None = None
    lon: float | None = None


class TripStopsResponse(BaseModel):
    trip_id: str
    route_id: str | None = None
    route_short_name: str | None = None
    headsign: str | None = None
    stops: list[TripStop]
