from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.orm import Session
import uvicorn

from knightbus.config import get_settings
from knightbus.db import get_db
from knightbus.logging import configure_logging
from knightbus.predictor import ETAPredictor
from knightbus.schemas import HealthResponse, StopEtaResponse, StopResponse, VehicleResponse

configure_logging()

app = FastAPI(title="Knightbus API", version="0.1.0")
predictor = ETAPredictor()


@app.get("/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(status="ok", environment=settings.app_env)


@app.get("/v1/stops/nearby", response_model=list[StopResponse])
def nearby_stops(
    lat: float = Query(..., description="User latitude"),
    lon: float = Query(..., description="User longitude"),
    radius_m: float = Query(default=500.0, description="Search radius in metres"),
    limit: int = Query(default=5, ge=1, le=20),
    db: Session = Depends(get_db),
) -> list[StopResponse]:
    return predictor.get_nearby_stops(db, lat=lat, lon=lon, radius_m=radius_m, limit=limit)


@app.get("/v1/stops/{stop_id}/etas", response_model=StopEtaResponse)
def stop_etas(
    stop_id: str,
    route_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> StopEtaResponse:
    items = predictor.get_stop_etas(db, stop_id=stop_id, route_id=route_id)
    if not items:
        raise HTTPException(status_code=404, detail=f"No ETA data found for stop {stop_id}")
    return StopEtaResponse(stop_id=stop_id, generated_at=datetime.now(timezone.utc), items=items)


@app.get("/v1/routes/{route_id}/vehicles", response_model=list[VehicleResponse])
def route_vehicles(route_id: str, db: Session = Depends(get_db)) -> list[VehicleResponse]:
    return [
        VehicleResponse(
            vehicle_id=row.vehicle_id,
            route_id=row.route_id,
            trip_id=row.trip_id,
            stop_id=row.stop_id,
            current_stop_sequence=row.current_stop_sequence,
            lat=row.lat,
            lon=row.lon,
            observed_at=row.observed_at,
        )
        for row in predictor.list_route_vehicles(db, route_id)
    ]


@app.get("/v1/models/current")
def current_model(db: Session = Depends(get_db)):
    return predictor.current_model_info(db)


def run() -> None:
    settings = get_settings()
    uvicorn.run(app, host=settings.app_host, port=settings.app_port)
