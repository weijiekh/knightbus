from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from knightbus import walking
from knightbus.main import app

PAYLOAD = {
    "features": [
        {
            "properties": {"summary": {"distance": 512.4, "duration": 410.2}},
            "geometry": {"coordinates": [[101.7, 3.15], [101.71, 3.151]]},
        }
    ]
}


@pytest.fixture(autouse=True)
def _reset_cache():
    walking._cache.clear()
    yield
    walking._cache.clear()


def _configure(monkeypatch, key: str | None = "test-key") -> None:
    monkeypatch.setattr(walking, "get_settings", lambda: SimpleNamespace(ors_api_key=key, ors_base_url="https://ors.test"))


def test_parse_ors_geojson_swaps_to_lat_lon() -> None:
    route = walking.parse_ors_geojson(PAYLOAD)

    assert route.distance_m == 512.4
    assert route.duration_s == 410.2
    assert route.geometry == [[3.15, 101.7], [3.151, 101.71]]


def test_parse_rejects_malformed_payload() -> None:
    with pytest.raises(walking.WalkingError):
        walking.parse_ors_geojson({"features": []})


def test_missing_key_is_unavailable(monkeypatch) -> None:
    _configure(monkeypatch, key=None)

    with pytest.raises(walking.WalkingUnavailable):
        walking.walking_route(3.1, 101.6, 3.2, 101.7)


def test_route_is_cached(monkeypatch) -> None:
    _configure(monkeypatch)
    calls: list[dict] = []

    def fake_post(url, json, headers, timeout):
        calls.append({"url": url, "json": json, "auth": headers["Authorization"]})
        return httpx.Response(200, json=PAYLOAD, request=httpx.Request("POST", url))

    monkeypatch.setattr(walking.httpx, "post", fake_post)

    first = walking.walking_route(3.15, 101.7, 3.16, 101.71)
    second = walking.walking_route(3.15, 101.7, 3.16, 101.71)

    assert first == second
    assert len(calls) == 1
    assert calls[0]["url"] == "https://ors.test/v2/directions/foot-walking/geojson"
    assert calls[0]["json"] == {"coordinates": [[101.7, 3.15], [101.71, 3.16]], "instructions": True, "language": "en"}
    assert calls[0]["auth"] == "test-key"


def test_upstream_error_maps_to_walking_error(monkeypatch) -> None:
    _configure(monkeypatch)

    def fake_post(url, json, headers, timeout):
        return httpx.Response(403, request=httpx.Request("POST", url))

    monkeypatch.setattr(walking.httpx, "post", fake_post)

    with pytest.raises(walking.WalkingError):
        walking.walking_route(3.15, 101.7, 3.16, 101.71)


def test_walk_endpoint_rejects_outside_service_area() -> None:
    client = TestClient(app)

    response = client.get("/v1/walk", params={"from_lat": 51.5, "from_lon": -0.1, "to_lat": 3.1, "to_lon": 101.7})

    assert response.status_code == 422


def test_walk_endpoint_returns_503_without_key(monkeypatch) -> None:
    _configure(monkeypatch, key=None)
    client = TestClient(app)

    response = client.get("/v1/walk", params={"from_lat": 3.15, "from_lon": 101.7, "to_lat": 3.16, "to_lon": 101.71})

    assert response.status_code == 503


def test_walk_endpoint_returns_ordered_turn_instructions(monkeypatch) -> None:
    from copy import deepcopy

    _configure(monkeypatch)
    payload = deepcopy(PAYLOAD)
    payload["features"][0]["properties"]["segments"] = [
        {"steps": [{"instruction": "Head north on Jalan SS 2", "distance": 120, "type": 11, "way_points": [0, 1]}]},
        {"steps": [
            {"instruction": "Turn left onto the footpath", "distance": 30, "type": 0},
            {"instruction": "Arrive at your destination", "distance": 0, "type": 10},
        ]},
    ]
    monkeypatch.setattr(walking.httpx, "post", lambda *a, **kw: httpx.Response(200, json=payload))
    response = TestClient(app).get("/v1/walk", params={"from_lat": 3.15, "from_lon": 101.7, "to_lat": 3.16, "to_lon": 101.71})

    assert response.status_code == 200
    assert response.json()["steps"] == [
        {"instruction": "Head north on Jalan SS 2", "distance_m": 120, "type": 11, "way_points": [0, 1]},
        {"instruction": "Turn left onto the footpath", "distance_m": 30, "type": 0, "way_points": None},
        {"instruction": "Arrive at your destination", "distance_m": 0, "type": 10, "way_points": None},
    ]


@pytest.mark.parametrize("segments", [None, [{"steps": [{"instruction": "Turn left"}]}], [{"steps": [{"instruction": " ", "distance": 10}]}]])
def test_bad_instructions_preserve_usable_map_route(segments) -> None:
    from copy import deepcopy

    payload = deepcopy(PAYLOAD)
    payload["features"][0]["properties"]["segments"] = segments
    route = walking.parse_ors_geojson(payload)
    assert route.steps == []
    assert route.distance_m == 512.4
    assert route.geometry == [[3.15, 101.7], [3.151, 101.71]]


@pytest.mark.parametrize("indices", [[-1, 1], [0, 50], [1, 0], [0], [0.5, 1], [False, 1], None])
def test_invalid_way_points_preserve_instruction_without_navigation_mapping(indices) -> None:
    from copy import deepcopy

    payload = deepcopy(PAYLOAD)
    payload["features"][0]["properties"]["segments"] = [{"steps": [
        {"instruction": "Turn left", "distance": 12, "type": 0, "way_points": indices},
    ]}]
    step = walking.parse_ors_geojson(payload).steps[0]
    assert step.instruction == "Turn left"
    assert step.way_points is None
