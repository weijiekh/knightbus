import logging

from sqlalchemy import Engine, text

logger = logging.getLogger(__name__)

# Idempotent DDL applied at worker startup. `create_all` only creates missing
# tables, so index drops and other schema changes live here.
MIGRATIONS: tuple[str, ...] = (
    # 94 MB, never scanned; single-column indexes cover the predictor lookups.
    "DROP INDEX IF EXISTS ix_vehicle_states_lookup",
)


def apply_migrations(engine: Engine) -> None:
    with engine.begin() as conn:
        for statement in MIGRATIONS:
            conn.execute(text(statement))
    logger.info("applied schema migrations", extra={"count": len(MIGRATIONS)})
