from knightbus.main import health


def test_health_endpoint() -> None:
    response = health()

    assert response.status == "ok"


def test_lifespan_starts_and_stops_embedded_scheduler_only_when_enabled(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    from knightbus import main, scheduler

    events: list[str] = []

    class FakeScheduler:
        def shutdown(self, wait: bool = True) -> None:
            events.append(f"shutdown wait={wait}")

    monkeypatch.setattr(scheduler, "start_background_scheduler", lambda: events.append("start") or FakeScheduler())

    async def run(enabled: bool) -> None:
        monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(embedded_scheduler=enabled))
        async with main.lifespan(main.app):
            events.append("serving")

    asyncio.run(run(False))
    assert events == ["serving"]

    events.clear()
    asyncio.run(run(True))
    assert events == ["start", "serving", "shutdown wait=False"]
