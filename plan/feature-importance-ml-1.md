---
goal: Feature Importance Layer for ML Model Input Understanding
version: 1.0
date_created: 2026-07-02
last_updated: 2026-07-02
owner: llm-market-scoring
status: 'Planned'
tags: [feature, ml, interpretability, sklearn, phase6]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

This plan introduces a **feature importance layer** to the ML pipeline (`app/ml/`). It enables
quantitative analysis of which input signals — LLM scores, LLM confidence, market baseline
features — actually drive the sklearn models' predictions for each asset/window combination.

Three sklearn-native methods are supported without new runtime dependencies:

| Method | Works For | Source |
|---|---|---|
| `permutation` | Any estimator | `sklearn.inspection.permutation_importance` |
| `builtin` | Tree-based (RF, GBM) | `model.feature_importances_` |
| `coefficient` | Linear (Ridge, Lasso, LogReg) | `model.coef_` |

Results are persisted to the existing `experiment_results` table and optionally exported to
Parquet for dashboard charting. The module is designed to slot into Phase 6 (`train.py`) with
minimal coupling — it receives a fitted estimator and a labeled feature matrix.

---

## 1. Requirements & Constraints

- **REQ-001**: Support three importance methods: `permutation`, `builtin`, `coefficient`. Each must be selectable at call-time via an enum value.
- **REQ-002**: `compute_importance()` must accept any fitted `sklearn.base.BaseEstimator` and return a typed `ImportanceResult` dataclass with `feature_names: list[str]`, `importances_mean: np.ndarray`, `importances_std: np.ndarray`, `method: FeatureImportanceMethod`.
- **REQ-003**: `get_top_features(result, n)` must return a list of `(feature_name, mean_importance)` tuples sorted descending by `abs(mean_importance)`, truncated to `n`.
- **REQ-004**: Results must be persistable to `experiment_results` using metric names formatted as `feat_imp::<feature_name>` (max 64 chars per ORM constraint; names exceeding 55 chars must be truncated to 52 chars and suffixed with `...`).
- **REQ-005**: Results must be loadable back from `experiment_results` into a `ImportanceResult`-equivalent structure via `load_importance_from_db(db, experiment_id, method)`.
- **REQ-006**: A Parquet export function `save_importance_to_parquet(result, path)` must write a DataFrame with columns `[feature, mean, std, method]`.
- **REQ-007**: `GET /ml/experiments/{experiment_id}/importance` must return ranked importance for a given experiment; response must include `method`, `feature`, `mean`, `std` fields.
- **REQ-008**: All computation must be executable offline with no network calls.
- **REQ-009**: All functions must be unit-testable with synthetic numpy data — no live DB or yfinance calls required in tests.
- **CON-001**: No new runtime Python package dependencies. Only `sklearn`, `numpy`, `pandas`, `pyarrow` (all already in `requirements.txt`).
- **CON-002**: `ExperimentResult.metric` column is `String(64)` — feature name prefix `feat_imp::` occupies 10 chars, leaving 54 chars for the feature name portion.
- **CON-003**: The `builtin` method raises `ValueError` if `model.feature_importances_` is absent (e.g. Ridge). The `coefficient` method raises `ValueError` if `model.coef_` is absent (e.g. RandomForest). Callers are responsible for selecting the correct method for the model type.
- **CON-004**: `permutation` importance requires a validation set `(X_val, y_val)` that must not overlap with the training set (enforced by the walk-forward split in `train.py`).
- **GUD-001**: Prefer `permutation` as the default method — it is model-agnostic and avoids the known bias of impurity-based importance toward high-cardinality features.
- **GUD-002**: `n_repeats` for permutation importance defaults to `10` (balances variance vs. compute time on a local 8-GB machine with a ~300-row aligned dataset).
- **GUD-003**: Use `random_state=42` as the default for permutation importance to ensure reproducibility across experiment runs.
- **PAT-001**: Follow the existing pattern in `app/ml/` — pure functions in module-level scope, no class state, typed with `from __future__ import annotations`.
- **PAT-002**: DB persistence uses the `ExperimentResult` ORM model imported from `app.db.models`; callers pass an active `Session` and commit externally.

---

## 2. Implementation Steps

### Implementation Phase 1 — Core Computation Module

- GOAL-001: Create `app/ml/importance.py` with all computation logic, dataclasses, and DB/Parquet I/O.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Create `app/ml/__init__.py` (empty, marks package). Path: `backend/app/ml/__init__.py`. | | |
| TASK-002 | Define `FeatureImportanceMethod` as `enum.Enum` with values `PERMUTATION = "permutation"`, `BUILTIN = "builtin"`, `COEFFICIENT = "coefficient"`. Place in `app/ml/importance.py`. | | |
| TASK-003 | Define `ImportanceResult` as a `dataclasses.dataclass(slots=True)` with fields: `feature_names: list[str]`, `importances_mean: np.ndarray`, `importances_std: np.ndarray`, `method: FeatureImportanceMethod`, `experiment_id: int \| None = None`. Place in `app/ml/importance.py`. | | |
| TASK-004 | Implement `_permutation_importance(model, X_val, y_val, feature_names, n_repeats, random_state) -> ImportanceResult`. Calls `sklearn.inspection.permutation_importance(model, X_val, y_val, n_repeats=n_repeats, random_state=random_state)`. Returns `ImportanceResult(feature_names=feature_names, importances_mean=result.importances_mean, importances_std=result.importances_std, method=FeatureImportanceMethod.PERMUTATION)`. | | |
| TASK-005 | Implement `_builtin_importance(model, feature_names) -> ImportanceResult`. Accesses `model.feature_importances_`; raises `ValueError(f"Model {type(model).__name__} has no feature_importances_ attribute")` if absent. `importances_std` is set to `np.zeros(len(feature_names))`. | | |
| TASK-006 | Implement `_coefficient_importance(model, feature_names) -> ImportanceResult`. Accesses `model.coef_`; raises `ValueError(f"Model {type(model).__name__} has no coef_ attribute")` if absent. Handles both 1-D (regression) and 2-D (multi-class) `coef_` by taking `np.abs(coef_).mean(axis=0)` for 2-D. `importances_std` is set to `np.zeros(len(feature_names))`. | | |
| TASK-007 | Implement the public entry point `compute_importance(model, feature_names, method, *, X_val=None, y_val=None, n_repeats=10, random_state=42) -> ImportanceResult`. Dispatches to the correct private function. Raises `ValueError` if `method == PERMUTATION` and `X_val` or `y_val` is `None`. | | |
| TASK-008 | Implement `get_top_features(result: ImportanceResult, n: int = 10) -> list[tuple[str, float]]`. Returns the top-`n` `(feature_name, mean_importance)` pairs sorted by `abs(mean_importance)` descending. | | |
| TASK-009 | Implement `_truncate_metric_name(feature_name: str) -> str`. Prefix: `"feat_imp::"` (10 chars). If `len(feature_name) > 54`: truncate to 51 chars and append `"..."`, giving a total metric string of ≤ 64 chars. | | |
| TASK-010 | Implement `save_importance_to_db(db: Session, experiment_id: int, result: ImportanceResult) -> int`. For each `(feature_name, mean_val, std_val)` in zip of result fields: insert one `ExperimentResult` row with `metric=_truncate_metric_name(feature_name)`, `value=float(mean_val)`, `fold=None`. Returns count of rows inserted. Does **not** call `db.commit()`. | | |
| TASK-011 | Implement `load_importance_from_db(db: Session, experiment_id: int) -> ImportanceResult \| None`. Queries `ExperimentResult` rows where `experiment_id == experiment_id` and `metric.startswith("feat_imp::")`. Reconstructs `feature_names` by stripping the prefix. Returns `None` if no rows found. `importances_std` is set to `np.zeros(n)` (std is not stored). `method` is set to `FeatureImportanceMethod.PERMUTATION` (stored method not recoverable from current schema — noted in RISK-001). | | |
| TASK-012 | Implement `save_importance_to_parquet(result: ImportanceResult, path: Path) -> None`. Constructs a `pd.DataFrame` with columns `["feature", "mean", "std", "method"]`. Calls `df.to_parquet(path, index=False)`. Creates parent directories if absent. | | |
| TASK-013 | Implement `importance_to_dict(result: ImportanceResult) -> list[dict]`. Returns `[{"feature": name, "mean": float(m), "std": float(s)} for name, m, s in zip(result.feature_names, result.importances_mean, result.importances_std)]` sorted by `abs(mean)` descending. Used by the API route. | | |

### Implementation Phase 2 — API Endpoint

- GOAL-002: Expose feature importance data through a `GET /ml/experiments/{experiment_id}/importance` endpoint, returnable by the React dashboard.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-014 | Create `app/api/routes/ml.py`. Define `router = APIRouter(prefix="/ml", tags=["ml"])`. | | |
| TASK-015 | Define Pydantic response models in `app/api/routes/ml.py`: `FeatureImportanceItem(feature: str, mean: float, std: float)` and `FeatureImportanceResponse(experiment_id: int, method: str, features: list[FeatureImportanceItem])`. | | |
| TASK-016 | Implement `GET /ml/experiments/{experiment_id}/importance` route in `app/api/routes/ml.py`. Calls `load_importance_from_db(db, experiment_id)`. Returns 404 if `None`. Calls `importance_to_dict(result)` and maps to `FeatureImportanceResponse`. | | |
| TASK-017 | Register the `ml` router in `app/main.py`: add `from app.api.routes.ml import router as ml_router` and `app.include_router(ml_router)` after the existing `score_router`. Add `"ml": "/ml"` to the root endpoint dict. | | |

### Implementation Phase 3 — Integration with `train.py`

- GOAL-003: Invoke `compute_importance` automatically after every model fit in `train.py`, persist results, and log the top-5 features.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-018 | When `app/ml/train.py` is created (Phase 6), add a call to `compute_importance(fitted_model, feature_names, method=FeatureImportanceMethod.PERMUTATION, X_val=X_val, y_val=y_val)` after each fold's `model.fit()`. | | |
| TASK-019 | After computing importance in `train.py`, call `save_importance_to_db(db, experiment_id, result)` before the fold's `db.commit()`. | | |
| TASK-020 | Log `get_top_features(result, n=5)` at `INFO` level in `train.py` using `log.info("Top-5 features for %s fold=%d: %s", model_name, fold, top)`. | | |

### Implementation Phase 4 — Tests

- GOAL-004: Full unit test coverage for `app/ml/importance.py` using synthetic numpy data; no live network or DB connections required for most tests.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-021 | Create `backend/tests/ml/__init__.py` (empty). | | |
| TASK-022 | Create `backend/tests/ml/test_importance.py`. Import `numpy as np`, `pytest`, and all public symbols from `app.ml.importance`. | | |
| TASK-023 | Test `compute_importance` with `method=PERMUTATION`: fit a `sklearn.ensemble.RandomForestRegressor` on 50 synthetic rows × 4 features. Assert `result.feature_names == feature_names`, `len(result.importances_mean) == 4`, all values are finite floats. | | |
| TASK-024 | Test `compute_importance` with `method=BUILTIN` on a fitted `RandomForestRegressor`. Assert `result.importances_mean.sum()` is approximately 1.0 (impurity-based importances sum to 1). | | |
| TASK-025 | Test `compute_importance` with `method=COEFFICIENT` on a fitted `sklearn.linear_model.Ridge`. Assert `len(result.importances_mean) == n_features`. | | |
| TASK-026 | Test `compute_importance` raises `ValueError` when `method=BUILTIN` on a `Ridge` model (no `feature_importances_`). | | |
| TASK-027 | Test `compute_importance` raises `ValueError` when `method=COEFFICIENT` on a `RandomForestRegressor` (no `coef_`). | | |
| TASK-028 | Test `compute_importance` raises `ValueError` when `method=PERMUTATION` and `X_val=None`. | | |
| TASK-029 | Test `get_top_features` returns exactly `n` items, sorted by `abs(mean)` descending. | | |
| TASK-030 | Test `get_top_features` with `n` greater than the number of features returns all features. | | |
| TASK-031 | Test `_truncate_metric_name` with a feature name of exactly 54 chars (no truncation), 55 chars (truncated to 51 + `...`). | | |
| TASK-032 | Test `save_importance_to_db` with an in-memory SQLite session (using the `db` fixture from `tests/conftest.py`): verify correct number of `ExperimentResult` rows inserted, correct `metric` prefix, correct `value` matching `importances_mean`. | | |
| TASK-033 | Test `load_importance_from_db` round-trips a saved `ImportanceResult`: save then load, assert `feature_names` and `importances_mean` arrays match (within float tolerance). | | |
| TASK-034 | Test `load_importance_from_db` returns `None` for an `experiment_id` with no importance rows. | | |
| TASK-035 | Test `save_importance_to_parquet` writes a valid Parquet file with correct columns and row count. Use `tmp_path` pytest fixture. | | |
| TASK-036 | Test `importance_to_dict` returns a list sorted by `abs(mean)` descending with correct keys. | | |

---

## 3. Alternatives

- **ALT-001**: Add a dedicated `feature_importance` DB table (`experiment_id`, `feature_name`, `mean`, `std`, `method`, `fold`) instead of reusing `ExperimentResult`. Rejected for Phase 1 to avoid a new Alembic migration; revisit if the 64-char metric name constraint causes real problems.
- **ALT-002**: Integrate SHAP (`shap` library) for model-agnostic, interaction-aware importance. Provides more accurate values for non-linear models. Rejected due to the new runtime dependency and slower computation (relevant for the 8-GB VRAM, CPU-limited machine). Noted in Phase 12 stretch goals.
- **ALT-003**: Store importance results only in Parquet (no DB persistence). Rejected because the API needs to serve importance data without loading Parquet files on every request, and the `ExperimentResult` table already exists.
- **ALT-004**: Compute importance inside `backtest.py` rather than `train.py`. Rejected because importance must be computed on the **validation fold**, which is available during training but not during the walk-forward backtest pass.

---

## 4. Dependencies

- **DEP-001**: `scikit-learn >= 1.5` — `sklearn.inspection.permutation_importance` (already in `requirements.txt`).
- **DEP-002**: `numpy >= 1.26` — array operations (already in `requirements.txt`).
- **DEP-003**: `pandas >= 2.2` — Parquet export via `DataFrame.to_parquet` (already in `requirements.txt`).
- **DEP-004**: `pyarrow >= 16.0` — Parquet engine used by pandas (already in `requirements.txt`).
- **DEP-005**: `app/db/models.py` — `ExperimentResult`, `Experiment` ORM classes (already defined).
- **DEP-006**: `app/ml/train.py` — must exist (Phase 6) before TASK-018, TASK-019, TASK-020 can be completed. These three tasks are **blocked** until Phase 6 begins.
- **DEP-007**: `app/api/routes/ml.py` — new file, no prior dependency. Can be created independently of Phase 6.

---

## 5. Files

- **FILE-001**: `backend/app/ml/__init__.py` — new; empty package marker.
- **FILE-002**: `backend/app/ml/importance.py` — new; contains `FeatureImportanceMethod`, `ImportanceResult`, `compute_importance`, `get_top_features`, `save_importance_to_db`, `load_importance_from_db`, `save_importance_to_parquet`, `importance_to_dict`, `_truncate_metric_name`.
- **FILE-003**: `backend/app/api/routes/ml.py` — new; contains `GET /ml/experiments/{experiment_id}/importance` route and Pydantic response models.
- **FILE-004**: `backend/app/main.py` — modified; add `ml_router` import and `app.include_router(ml_router)`.
- **FILE-005**: `backend/app/ml/train.py` — modified (when created in Phase 6); add `compute_importance` call after each fold fit.
- **FILE-006**: `backend/tests/ml/__init__.py` — new; empty test package marker.
- **FILE-007**: `backend/tests/ml/test_importance.py` — new; 14 unit tests (TASK-022 through TASK-036).

---

## 6. Testing

- **TEST-001**: `test_permutation_importance_shape` — verifies `ImportanceResult` dimensions match input feature count (TASK-023).
- **TEST-002**: `test_builtin_importance_sums_to_one` — verifies impurity-based importances from RF sum to ≈ 1.0 (TASK-024).
- **TEST-003**: `test_coefficient_importance` — verifies coefficient-based importance length equals feature count (TASK-025).
- **TEST-004**: `test_builtin_raises_for_linear_model` — verifies `ValueError` on Ridge (TASK-026).
- **TEST-005**: `test_coefficient_raises_for_tree_model` — verifies `ValueError` on RandomForest (TASK-027).
- **TEST-006**: `test_permutation_raises_without_val_data` — verifies `ValueError` when `X_val=None` (TASK-028).
- **TEST-007**: `test_get_top_features_sorted_and_truncated` — verifies correct count and descending `abs` order (TASK-029).
- **TEST-008**: `test_get_top_features_clamps_to_n_features` — verifies no IndexError when `n > len(features)` (TASK-030).
- **TEST-009**: `test_truncate_metric_name_boundaries` — verifies exact 54-char passthrough and 55-char truncation (TASK-031).
- **TEST-010**: `test_save_importance_to_db_rows` — verifies correct row count and metric formatting in DB (TASK-032).
- **TEST-011**: `test_load_importance_roundtrip` — verifies feature names and means survive save → load (TASK-033).
- **TEST-012**: `test_load_importance_returns_none_for_missing` — verifies `None` on missing experiment (TASK-034).
- **TEST-013**: `test_save_importance_to_parquet` — verifies Parquet file columns and row count (TASK-035).
- **TEST-014**: `test_importance_to_dict_sorted` — verifies dict output is sorted by abs mean (TASK-036).

---

## 7. Risks & Assumptions

- **RISK-001**: `load_importance_from_db` cannot recover the original `method` because it is not stored in `ExperimentResult`. The loaded `ImportanceResult` always reports `method=PERMUTATION`. If method provenance matters downstream (e.g. for the dashboard legend), store it as a separate `ExperimentResult` row with `metric="feat_imp::__method__"` and `value=0.0` and `fold=None`; or defer to ALT-001 (dedicated table).
- **RISK-002**: Permutation importance on a ~300-row aligned dataset (Phase 5 output) with `n_repeats=10` takes approximately 0.1–2 seconds per model per fold depending on the sklearn estimator. Acceptable for local use; revisit if feature count grows beyond 50.
- **RISK-003**: `ExperimentResult` rows are inserted without a unique constraint on `(experiment_id, metric)`. Re-running training on the same experiment will insert duplicate importance rows. Mitigate by deleting existing `feat_imp::*` rows for the experiment before calling `save_importance_to_db`.
- **RISK-004**: TASK-018, TASK-019, TASK-020 cannot be implemented until `app/ml/train.py` exists (Phase 6 dependency). These tasks should be tracked as blocked until Phase 6 begins.
- **ASSUMPTION-001**: The feature matrix produced by `app/ml/features.py` (Phase 6) will have named columns accessible as a list of strings (`feature_names`) that can be passed directly to `compute_importance`.
- **ASSUMPTION-002**: The walk-forward CV splits in `train.py` will expose a validation set `(X_val, y_val)` per fold at the point of calling `compute_importance`, consistent with the leakage-free requirement in CON-004.
- **ASSUMPTION-003**: The `Experiment` row is created and its `id` is known before `save_importance_to_db` is called. This is consistent with the existing DB design where experiments are registered before results are stored.

---

## 8. Related Specifications / Further Reading

- [TODO.md — Phase 6 ML Layer](../TODO.md#phase-6--ml-layer-sklearn-swappable) — defines the feature matrix schema, sklearn model registry, and walk-forward CV approach that this plan extends.
- [TODO.md — Phase 5 Score ↔ Return Alignment](../TODO.md#phase-5--score--return-alignment) — produces the aligned dataset that serves as the feature importance input.
- [sklearn: permutation_importance](https://scikit-learn.org/stable/modules/permutation_importance.html) — official docs for the primary method used.
- [sklearn: feature_importances_](https://scikit-learn.org/stable/auto_examples/ensemble/plot_forest_importances.html) — notes on impurity bias vs. permutation accuracy.
- [app/db/models.py](../backend/app/db/models.py) — `Experiment`, `ExperimentResult` ORM definitions referenced throughout this plan.
