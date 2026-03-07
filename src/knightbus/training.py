import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from knightbus.models import ModelVersion


class TrainingService:
    def run_training(self, db: Session) -> ModelVersion:
        version = datetime.now(timezone.utc).strftime("baseline-%Y%m%d%H%M%S")
        model = ModelVersion(
            version=version,
            status="baseline_only",
            metrics_json=json.dumps(
                {
                    "message": "ML training is not enabled yet. The service currently serves schedule and heuristic ETAs.",
                }
            ),
        )
        db.add(model)
        db.commit()
        db.refresh(model)
        return model


def main() -> None:
    from knightbus.db import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        model = TrainingService().run_training(db)
        print(model.version)
