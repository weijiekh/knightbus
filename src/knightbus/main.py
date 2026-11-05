from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
import uvicorn

from knightbus.config import get_settings
from knightbus.db import get_db
from knightbus.logging import configure_logging
from knightbus.predictor import ETAPredictor
from knightbus.models import Route, Stop
from knightbus.search import search as search_stops_and_routes, split_stop_name
from knightbus.schemas import HealthResponse, SearchResponse, SearchRoute, StopEtaResponse, StopResponse, TripJourneyResponse, TripStopsResponse, VehicleResponse, WalkRouteResponse
from knightbus.vehicles import route_vehicles as list_route_vehicles
from knightbus.trips import get_trip_journey, get_trip_stops
from knightbus.walking import WalkingError, WalkingUnavailable, in_service_area, walking_route

configure_logging()

@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler = None
    if get_settings().embedded_scheduler:
        # Imported here so a plain API process never loads the scheduler stack.
        from knightbus.scheduler import start_background_scheduler

        scheduler = start_background_scheduler()
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Knightbus API", version="0.1.0", lifespan=lifespan)
predictor = ETAPredictor()
MAX_BATCH_STOPS = 10


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


@app.get("/v1/search", response_model=SearchResponse)
def search(
    q: str = Query(default="", description="Search text (stops and routes)"),
    lat: float | None = Query(default=None),
    lon: float | None = Query(default=None),
    limit: int = Query(default=8, ge=1, le=20),
    db: Session = Depends(get_db),
) -> SearchResponse:
    return search_stops_and_routes(db, q, lat=lat, lon=lon, limit=limit)


def _stop_eta_response(db: Session, stop_id: str, route_id: str | None = None) -> StopEtaResponse | None:
    items = predictor.get_stop_etas(db, stop_id=stop_id, route_id=route_id)
    if not items:
        return None
    stop = db.query(Stop).filter(Stop.stop_id == stop_id).first()
    code, name = split_stop_name(stop.name) if stop else (None, None)
    return StopEtaResponse(
        stop_id=stop_id,
        stop_name=name,
        stop_code=code,
        lat=stop.lat if stop else None,
        lon=stop.lon if stop else None,
        generated_at=datetime.now(timezone.utc),
        items=items,
    )


@app.get("/v1/stops/etas", response_model=list[StopEtaResponse])
def batch_stop_etas(
    stop_ids: str = Query(..., description="Comma-separated stop ids (max 10)"),
    db: Session = Depends(get_db),
) -> list[StopEtaResponse]:
    """ETAs for several stops in one request/DB session (nearby list). Stops without data
    come back with empty `items` instead of failing the whole batch."""
    ids = list(dict.fromkeys(i.strip() for i in stop_ids.split(",") if i.strip()))
    if not ids or len(ids) > MAX_BATCH_STOPS:
        raise HTTPException(status_code=422, detail=f"stop_ids must contain 1-{MAX_BATCH_STOPS} ids")
    return [
        _stop_eta_response(db, stop_id)
        or StopEtaResponse(stop_id=stop_id, generated_at=datetime.now(timezone.utc), items=[])
        for stop_id in ids
    ]


@app.get("/v1/stops/{stop_id}/etas", response_model=StopEtaResponse)
def stop_etas(
    stop_id: str,
    route_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> StopEtaResponse:
    response = _stop_eta_response(db, stop_id, route_id)
    if response is None:
        raise HTTPException(status_code=404, detail=f"No ETA data found for stop {stop_id}")
    return response


@app.get("/v1/trips/{trip_id}/stops", response_model=TripStopsResponse)
def trip_stops(trip_id: str, db: Session = Depends(get_db)) -> TripStopsResponse:
    result = get_trip_stops(db, trip_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No stops found for trip {trip_id}")
    return result


@app.get("/v1/trips/{trip_id}/journey", response_model=TripJourneyResponse)
def trip_journey(trip_id: str, db: Session = Depends(get_db)) -> TripJourneyResponse:
    result = get_trip_journey(db, trip_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No stops found for trip {trip_id}")
    return result


@app.get("/v1/walk", response_model=WalkRouteResponse)
def walk(
    from_lat: float = Query(ge=-90, le=90),
    from_lon: float = Query(ge=-180, le=180),
    to_lat: float = Query(ge=-90, le=90),
    to_lon: float = Query(ge=-180, le=180),
) -> WalkRouteResponse:
    if not (in_service_area(from_lat, from_lon) and in_service_area(to_lat, to_lon)):
        raise HTTPException(status_code=422, detail="outside service area")
    try:
        route = walking_route(from_lat, from_lon, to_lat, to_lon)
    except WalkingUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except WalkingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return WalkRouteResponse(distance_m=route.distance_m, duration_s=route.duration_s, geometry=route.geometry)


@app.get("/v1/routes/{route_id}/vehicles", response_model=list[VehicleResponse])
def route_vehicles(route_id: str, db: Session = Depends(get_db)) -> list[VehicleResponse]:
    return list_route_vehicles(db, route_id)


@app.get("/v1/routes/{route_id}", response_model=SearchRoute)
def route_details(route_id: str, db: Session = Depends(get_db)) -> SearchRoute:
    route = db.execute(select(Route).where(Route.route_id == route_id).order_by(Route.id)).scalars().first()
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    return SearchRoute(route_id=route.route_id, short_name=route.short_name, long_name=route.long_name, feed_name=route.feed_name)


@app.get("/v1/models/current")
def current_model(db: Session = Depends(get_db)):
    return predictor.current_model_info(db)


def run() -> None:
    settings = get_settings()
    uvicorn.run(app, host=settings.app_host, port=settings.app_port)
