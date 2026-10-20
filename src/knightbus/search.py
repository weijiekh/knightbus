from __future__ import annotations

import re
from math import cos, radians

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from knightbus.models import Route, Stop, StopTime, Trip
from knightbus.schemas import SearchResponse, SearchRoute, SearchStop
from knightbus.utils import haversine_meters

MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 64
MAX_ROUTES_PER_STOP = 6
# SQL pre-ranks coarsely; Python refines the tier on this many candidates.
CANDIDATE_FACTOR = 10
MIN_CANDIDATES = 100

_CODE_RE = re.compile(r"^[A-Z]{1,4}\d+$")


def split_stop_name(raw: str | None) -> tuple[str | None, str | None]:
    """Split "KL105 LRT MASJID JAMEK" into ("KL105", "LRT MASJID JAMEK")."""
    if not raw:
        return None, raw
    head, _, rest = raw.strip().partition(" ")
    if rest and _CODE_RE.match(head.upper()):
        return head, rest.strip()
    return None, raw


def _natural_key(value: str) -> list[tuple[int, int, str]]:
    return [
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.findall(r"\d+|\D+", value)
    ]


def _stop_tier(q: str, code: str | None, name: str | None) -> int:
    if code and code.lower() == q:
        return 0
    lowered = (name or "").lower()
    if lowered.startswith(q) or (code and code.lower().startswith(q)):
        return 1
    if any(word.startswith(q) for word in lowered.split()):
        return 2
    return 3


def _search_stops(
    db: Session, q: str, lat: float | None, lon: float | None, limit: int
) -> list[SearchStop]:
    lowered = func.lower(Stop.name)
    coarse_tier = case(
        (or_(lowered == q, lowered.like(f"{q} %")), 0),
        (lowered.startswith(q, autoescape=True), 1),
        (lowered.contains(f" {q}", autoescape=True), 2),
        else_=3,
    )
    order = [coarse_tier]
    if lat is not None and lon is not None:
        lon_scale = cos(radians(lat)) ** 2
        order += [
            case((or_(Stop.lat.is_(None), Stop.lon.is_(None)), 1), else_=0),
            (Stop.lat - lat) * (Stop.lat - lat) + (Stop.lon - lon) * (Stop.lon - lon) * lon_scale,
        ]
    order.append(lowered)

    rows = db.execute(
        select(Stop)
        .where(lowered.contains(q, autoescape=True))
        .order_by(*order)
        .limit(max(limit * CANDIDATE_FACTOR, MIN_CANDIDATES))
    ).scalars().all()

    ranked = []
    for stop in rows:
        code, name = split_stop_name(stop.name)
        distance = None
        if lat is not None and lon is not None and stop.lat is not None and stop.lon is not None:
            distance = round(haversine_meters(lat, lon, stop.lat, stop.lon), 1)
        sort_distance = distance if distance is not None else float("inf")
        sort_tail = sort_distance if lat is not None and lon is not None else 0.0
        ranked.append(
            (
                (_stop_tier(q, code, name), sort_tail, (stop.name or "").lower()),
                SearchStop(
                    stop_id=stop.stop_id,
                    code=code,
                    name=name,
                    lat=stop.lat,
                    lon=stop.lon,
                    distance_m=distance,
                ),
                stop.id,
            )
        )
    ranked.sort(key=lambda item: item[0])
    page = ranked[:limit]

    routes_by_stop = _routes_for_stops(db, [item[2] for item in page])
    for _, result, pk in page:
        result.routes = routes_by_stop.get(pk, [])
    return [item[1] for item in page]


def _routes_for_stops(db: Session, stop_pks: list[int]) -> dict[int, list[str]]:
    if not stop_pks:
        return {}
    rows = db.execute(
        select(StopTime.stop_id_ref, Route.short_name)
        .join(Trip, Trip.id == StopTime.trip_id_ref)
        .join(Route, (Route.feed_name == Trip.feed_name) & (Route.route_id == Trip.route_id))
        .where(StopTime.stop_id_ref.in_(stop_pks), Route.short_name.is_not(None))
        .distinct()
    ).all()
    grouped: dict[int, set[str]] = {}
    for stop_pk, short_name in rows:
        grouped.setdefault(stop_pk, set()).add(short_name)
    return {
        pk: sorted(names, key=_natural_key)[:MAX_ROUTES_PER_STOP] for pk, names in grouped.items()
    }


def _search_routes(db: Session, q: str, limit: int) -> list[SearchRoute]:
    short = func.lower(Route.short_name)
    long_ = func.lower(Route.long_name)
    tier = case(
        (short == q, 0),
        (short.startswith(q, autoescape=True), 1),
        else_=2,
    )
    rows = db.execute(
        select(Route)
        .where(or_(short.startswith(q, autoescape=True), long_.contains(q, autoescape=True)))
        .order_by(tier, short, Route.feed_name, Route.route_id)
        .limit(limit)
    ).scalars().all()
    return [
        SearchRoute(
            route_id=r.route_id,
            short_name=r.short_name,
            long_name=r.long_name,
            feed_name=r.feed_name,
        )
        for r in rows
    ]


def search(
    db: Session,
    q: str,
    lat: float | None = None,
    lon: float | None = None,
    limit: int = 8,
) -> SearchResponse:
    query = q.strip()[:MAX_QUERY_LENGTH].strip()
    if len(query) < MIN_QUERY_LENGTH:
        return SearchResponse(query=query, stops=[], routes=[])
    needle = query.lower()
    return SearchResponse(
        query=query,
        stops=_search_stops(db, needle, lat, lon, limit),
        routes=_search_routes(db, needle, limit),
    )
