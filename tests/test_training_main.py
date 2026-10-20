import runpy
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from knightbus import db as db_module
from knightbus.models import ModelVersion


def test_module_invocation_runs_main_and_reports_status(monkeypatch, capsys) -> None:  # noqa: ANN001
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", session_factory)

    monkeypatch.delitem(sys.modules, "knightbus.training", raising=False)
    runpy.run_module("knightbus.training", run_name="__main__")

    with session_factory() as session:
        rows = session.execute(select(ModelVersion)).scalars().all()
    assert len(rows) == 1
    out = capsys.readouterr().out
    assert rows[0].version in out
    assert rows[0].status in out
