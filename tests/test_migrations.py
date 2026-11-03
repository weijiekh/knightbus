from sqlalchemy import create_engine, text

from knightbus.migrations import apply_migrations


def test_migrations_are_idempotent_and_drop_unused_index() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE vehicle_states (stop_id TEXT, trip_id TEXT, observed_at TEXT)"))
        conn.execute(text("CREATE TABLE stops (lat REAL, lon REAL)"))
        conn.execute(text("CREATE TABLE trip_update_states (stop_id TEXT, observed_at TEXT)"))
        conn.execute(text("CREATE INDEX ix_vehicle_states_lookup ON vehicle_states (stop_id, trip_id, observed_at)"))

    apply_migrations(engine)
    apply_migrations(engine)

    with engine.connect() as conn:
        names = conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).scalars().all()
    assert "ix_vehicle_states_lookup" not in names


def test_migrations_drop_redundant_vehicle_indexes() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE vehicle_states (feed_name TEXT, vehicle_id TEXT, stop_id TEXT)"))
        conn.execute(text("CREATE TABLE stops (lat REAL, lon REAL)"))
        conn.execute(text("CREATE TABLE trip_update_states (stop_id TEXT, observed_at TEXT)"))
        for col in ("feed_name", "vehicle_id", "stop_id"):
            conn.execute(text(f"CREATE INDEX ix_vehicle_states_{col} ON vehicle_states ({col})"))

    apply_migrations(engine)
    apply_migrations(engine)

    with engine.connect() as conn:
        names = conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).scalars().all()
    assert sorted(names) == ["ix_stops_lat_lon", "ix_trip_updates_stop_recent"]
