from knightbus.main import health


def test_health_endpoint() -> None:
    response = health()

    assert response.status == "ok"
