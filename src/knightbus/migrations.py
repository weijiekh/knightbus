import logging

from sqlalchemy import Engine, text

logger = logging.getLogger(__name__)

# Idempotent DDL applied at worker startup. `create_all` only creates missing
# tables, so index drops and other schema changes live here.
MIGRATIONS: tuple[str, ...] = (
    # 94 MB, never scanned; single-column indexes cover the predictor lookups.
    "DROP INDEX IF EXISTS ix_vehicle_states_lookup",
    # Never used as a filter on their own; each is tens of MB competing for the
    # db container's page cache. The hot lookups use trip_id, route_id, observed_at.
    "DROP INDEX IF EXISTS ix_vehicle_states_feed_name",
    "DROP INDEX IF EXISTS ix_vehicle_states_vehicle_id",
    "DROP INDEX IF EXISTS ix_vehicle_states_stop_id",
    # create_all won't add an index to an existing table.
    "CREATE INDEX IF NOT EXISTS ix_stops_lat_lon ON stops (lat, lon)",
)


def apply_migrations(engine: Engine) -> None:
    with engine.begin() as conn:
        for statement in MIGRATIONS:
            conn.execute(text(statement))
    logger.info("applied schema migrations", extra={"count": len(MIGRATIONS)})
