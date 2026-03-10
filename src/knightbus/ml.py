from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ResidualModel:
    model: Any
    feature_columns: list[str]
    categorical_columns: list[str]

    def predict(self, features: dict[str, object]) -> float:
        try:
            import pandas as pd
        except ImportError as exc:
            raise RuntimeError("pandas is required for ML predictions") from exc

        row = {col: features.get(col) for col in self.feature_columns}
        df = pd.DataFrame([row])
        for col in self.categorical_columns:
            if col in df.columns:
                df[col] = df[col].astype("category")
        return float(self.model.predict(df)[0])


def load_residual_model(path: str) -> ResidualModel | None:
    model_path = Path(path)
    if not model_path.exists():
        return None

    try:
        with model_path.open("rb") as handle:
            payload = pickle.load(handle)
    except Exception:
        logger.exception("failed loading ML model", extra={"path": str(model_path)})
        return None

    model = payload.get("model")
    feature_columns = payload.get("feature_columns")
    categorical_columns = payload.get("categorical_columns", [])
    if model is None or not feature_columns:
        logger.warning("invalid ML model payload", extra={"path": str(model_path)})
        return None

    return ResidualModel(
        model=model,
        feature_columns=list(feature_columns),
        categorical_columns=list(categorical_columns),
    )


def save_residual_model(
    path: str,
    model: Any,
    feature_columns: list[str],
    categorical_columns: list[str],
    metrics: dict[str, object],
) -> None:
    model_path = Path(path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": model,
        "feature_columns": feature_columns,
        "categorical_columns": categorical_columns,
        "metrics": metrics,
    }
    with model_path.open("wb") as handle:
        pickle.dump(payload, handle)
