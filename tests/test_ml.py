import pytest

lgb = pytest.importorskip("lightgbm")
pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

from knightbus.ml import load_residual_model, save_residual_model


def fit_model():
    rng = np.random.default_rng(0)
    n = 400
    df = pd.DataFrame({
        "route_id": rng.choice(["r1", "r2", "r3"], n),
        "hour": rng.integers(0, 24, n).astype(float),
    })
    df["route_id"] = df["route_id"].astype("category")
    y = (df["route_id"].cat.codes * 30 + df["hour"]).astype(float)
    model = lgb.LGBMRegressor(n_estimators=20, min_child_samples=5, verbose=-1)
    model.fit(df, y, categorical_feature=["route_id"])
    return model, df


def test_numpy_predict_matches_sklearn_predict(tmp_path) -> None:
    model, df = fit_model()
    path = tmp_path / "m.pkl"
    save_residual_model(str(path), model, ["route_id", "hour"], ["route_id"], metrics={})

    loaded = load_residual_model(str(path))

    assert loaded is not None
    for route, hour in [("r1", 3.0), ("r3", 20.0)]:
        expected = model.predict(pd.DataFrame({"route_id": pd.Categorical([route], categories=df["route_id"].cat.categories), "hour": [hour]}))[0]
        assert loaded.predict({"route_id": route, "hour": hour}) == pytest.approx(expected, rel=1e-6)


def test_unseen_category_and_missing_numeric_do_not_crash(tmp_path) -> None:
    model, _ = fit_model()
    path = tmp_path / "m.pkl"
    save_residual_model(str(path), model, ["route_id", "hour"], ["route_id"], metrics={})
    loaded = load_residual_model(str(path))
    assert isinstance(loaded.predict({"route_id": "never-seen", "hour": None}), float)


def test_old_pickle_format_returns_none(tmp_path) -> None:
    import pickle
    path = tmp_path / "old.pkl"
    path.write_bytes(pickle.dumps({"model": object(), "feature_columns": ["a"]}))
    assert load_residual_model(str(path)) is None
