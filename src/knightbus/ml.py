from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


FORMAT = "lgbm-booster-v1"


@dataclass
class ResidualModel:
    booster: Any
    feature_columns: list[str]
    categorical_columns: list[str]
    categories: dict[str, list[str]]

    def _encode(self, col: str, value: object) -> float:
        import numpy as np

        if col in self.categorical_columns:
            # Training fills missing categoricals with "unknown"; unseen values stay missing.
            key = "unknown" if value is None else str(value)
            try:
                return float(self.categories.get(col, []).index(key))
            except ValueError:
                return float(np.nan)
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
        return 0.0 if number != number else number

    def predict(self, features: dict[str, object]) -> float:
        import numpy as np

        row = np.array(
            [[self._encode(col, features.get(col)) for col in self.feature_columns]],
            dtype=float,
        )
        return float(self.booster.predict(row)[0])


def load_residual_model(path: str) -> ResidualModel | None:
    model_path = Path(path)
    if not model_path.exists():
        return None

    try:
        import lightgbm as lgb

        with model_path.open("rb") as handle:
            payload = pickle.load(handle)
        if not isinstance(payload, dict) or payload.get("format") != FORMAT:
            logger.warning("unsupported ML model format; retrain required", extra={"path": str(model_path)})
            return None
        booster = lgb.Booster(model_str=payload["model_str"])
        return ResidualModel(
            booster=booster,
            feature_columns=list(payload["feature_columns"]),
            categorical_columns=list(payload.get("categorical_columns", [])),
            categories={col: list(vals) for col, vals in payload.get("categories", {}).items()},
        )
    except Exception:
        logger.exception("failed loading ML model", extra={"path": str(model_path)})
        return None


def save_residual_model(
    path: str,
    model: Any,
    feature_columns: list[str],
    categorical_columns: list[str],
    metrics: dict[str, object],
) -> None:
    model_path = Path(path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    booster = model.booster_
    ordered_cats = [col for col in feature_columns if col in categorical_columns]
    categories = {
        col: [str(v) for v in values]
        for col, values in zip(ordered_cats, booster.pandas_categorical or [])
    }
    payload = {
        "format": FORMAT,
        "model_str": booster.model_to_string(),
        "feature_columns": list(feature_columns),
        "categorical_columns": list(categorical_columns),
        "categories": categories,
        "metrics": metrics,
    }
    with model_path.open("wb") as handle:
        pickle.dump(payload, handle)
