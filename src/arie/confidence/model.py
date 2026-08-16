"""The calibrated confidence model.

Fitted **only** on the calibration split. The test split is never read here, not
even to check a metric — a threshold tuned against test data would report a
guarantee it could not keep in production.

Three-way company-disjoint partition of the calibration split:

  ``fit`` ─── base classifier, via grouped cross-validation
  ``cal`` ─── isotonic/Platt mapping, fitted on out-of-fold scores
  ``tau`` ─── held out entirely; used only to measure ECE and pick τ

Grouping is by **company**, not by lead or by state. All of a company's contacts
share its cached company-level evidence, and one lead contributes many
enrichment states, so splitting at any finer granularity would leak: the model
would be scored on states of companies it had already learned.

The model is refit deterministically rather than pickled. It trains in a second
or two on a few thousand rows, and a checked-in artefact would eventually drift
out of sync with the code that produced it.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

from arie.confidence.features import FEATURE_NAMES, feature_vector
from arie.confidence.metrics import CalibrationReport, calibration_report
from arie.confidence.threshold import DEFAULT_DELTA, ThresholdSelection, select_threshold
from arie.evalgen.schema import EvalLead
from arie.providers.catalog import CATALOG
from arie.scoring.engine import ScoringResult, score_resolved
from arie.scoring.merge import candidates_from_observations, facts_from, resolve_candidates

CalibrationMethod = str  # "isotonic" | "platt"

N_CV_FOLDS = 5
N_RANDOM_STATES_PER_LEAD = 6
DEFAULT_TAU_HOLDOUT_FRACTION = 0.35


def enrichment_states(lead: EvalLead, seed: int) -> Iterator[frozenset[str]]:
    """Provider subsets representing points partway through enrichment.

    Two families, deliberately:

    * **Prefixes** of the catalogue's cheapest-first order — the states a
      waterfall baseline actually visits.
    * **Seeded random subsets** — because the adaptive policy selects by
      expected value, not by price order, and will reach combinations no prefix
      covers. Training only on prefixes would leave the model extrapolating
      exactly where it is most relied upon.
    """
    names = [spec.name for spec in CATALOG]

    for i in range(len(names) + 1):
        yield frozenset(names[:i])

    rng = random.Random(seed)
    for _ in range(N_RANDOM_STATES_PER_LEAD):
        size = rng.randint(1, len(names))
        yield frozenset(rng.sample(names, size))


def score_at_state(lead: EvalLead, providers: Iterable[str]) -> ScoringResult:
    """Score a lead as if only `providers` had been called."""
    resolutions = resolve_candidates(candidates_from_observations(lead.observations, providers))
    return score_resolved(facts_from(resolutions), resolutions)


@dataclass(frozen=True)
class TrainingFrame:
    features: np.ndarray
    labels: np.ndarray
    groups: np.ndarray
    lead_ids: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.labels)


def build_training_frame(leads: Sequence[EvalLead], seed: int = 7) -> TrainingFrame:
    """Expand leads into (enrichment state -> was the decision correct?) rows.

    Leads are sorted and their sampling seeds derived from lead *identity*
    rather than list position, so the frame — and therefore the fitted model —
    is a pure function of the lead set. Seeding by position would make the
    threshold depend on the order the caller happened to pass leads in, which
    is the kind of irreproducibility that is invisible until someone else tries
    to replicate a published number.
    """
    rows: list[list[float]] = []
    labels: list[int] = []
    groups: list[str] = []
    lead_ids: list[str] = []

    for lead in sorted(leads, key=lambda x: x.eval_lead_id):
        state_seed = int(
            hashlib.sha256(f"{seed}:{lead.eval_lead_id}".encode()).hexdigest()[:12], 16
        )
        for state in enrichment_states(lead, state_seed):
            result = score_at_state(lead, state)
            rows.append(feature_vector(result))
            labels.append(int(result.decision == lead.oracle_decision))
            groups.append(lead.company.company_id)
            lead_ids.append(lead.eval_lead_id)

    return TrainingFrame(
        features=np.asarray(rows, dtype=float),
        labels=np.asarray(labels, dtype=int),
        groups=np.asarray(groups, dtype=object),
        lead_ids=tuple(lead_ids),
    )


def _group_folds(groups: np.ndarray, n_folds: int) -> list[np.ndarray]:
    """Partition row indices into folds that never split a company.

    Hand-rolled rather than using GroupKFold so the assignment is explicit and
    obviously deterministic: companies are sorted, then dealt round-robin.
    """
    unique = sorted({str(g) for g in groups})
    assignment = {name: i % n_folds for i, name in enumerate(unique)}
    return [
        np.array([i for i, g in enumerate(groups) if assignment[str(g)] == fold])
        for fold in range(n_folds)
    ]


@dataclass
class ConfidenceModel:
    """Predicts P(the decision I would make now matches the oracle)."""

    base: LogisticRegression
    calibrator: Any
    method: CalibrationMethod
    report: CalibrationReport
    threshold: ThresholdSelection
    feature_names: tuple[str, ...] = FEATURE_NAMES

    def _raw(self, features: np.ndarray) -> np.ndarray:
        return np.asarray(self.base.predict_proba(features)[:, 1], dtype=float)

    def predict_many(self, features: np.ndarray) -> np.ndarray:
        calibrated = self.calibrator.predict(self._raw(features))
        return np.clip(np.asarray(calibrated, dtype=float), 0.0, 1.0)

    def predict(self, result: ScoringResult) -> float:
        return float(self.predict_many(np.asarray([feature_vector(result)], dtype=float))[0])

    @property
    def tau(self) -> float:
        return self.threshold.tau

    def is_autonomous(self, result: ScoringResult) -> bool:
        """Whether this decision clears the bar for acting without a human."""
        return self.predict(result) >= self.tau

    def coefficients(self) -> dict[str, float]:
        return {
            name: round(float(coef), 6)
            for name, coef in zip(self.feature_names, self.base.coef_[0], strict=True)
        }


def _fit_calibrator(method: CalibrationMethod, raw: np.ndarray, labels: np.ndarray) -> Any:
    if method == "isotonic":
        model = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        model.fit(raw, labels)
        return model
    if method == "platt":
        platt = LogisticRegression(C=1e10, solver="lbfgs", max_iter=1000)
        platt.fit(raw.reshape(-1, 1), labels)

        class _PlattWrapper:
            def __init__(self, inner: LogisticRegression) -> None:
                self._inner = inner

            def predict(self, values: np.ndarray) -> np.ndarray:
                return np.asarray(
                    self._inner.predict_proba(np.asarray(values).reshape(-1, 1))[:, 1],
                    dtype=float,
                )

        return _PlattWrapper(platt)
    raise ValueError(f"unknown calibration method: {method!r}")


def _split_companies(
    leads: Sequence[EvalLead], holdout_fraction: float
) -> tuple[list[EvalLead], list[EvalLead]]:
    """Company-disjoint split. Deterministic: sorted, then strided."""
    companies = sorted({lead.company.company_id for lead in leads})
    stride = max(1, round(1 / holdout_fraction)) if holdout_fraction > 0 else len(companies) + 1
    holdout = {name for i, name in enumerate(companies) if i % stride == 0}
    return (
        [x for x in leads if x.company.company_id not in holdout],
        [x for x in leads if x.company.company_id in holdout],
    )


def fit_confidence_model(
    calibration_leads: Sequence[EvalLead],
    target_error_rate: float,
    method: CalibrationMethod = "auto",
    delta: float = DEFAULT_DELTA,
    tau_holdout_fraction: float = DEFAULT_TAU_HOLDOUT_FRACTION,
    seed: int = 7,
) -> ConfidenceModel:
    """Fit base model, calibrator, and τ — using only the calibration split.

    With ``method="auto"`` both calibrators are fitted and the one that
    automates the most while still meeting the error guarantee is kept.

    That selection rule is deliberate. Isotonic regression usually achieves the
    lower ECE, but it outputs a coarse step function: large blocks of
    predictions collapse to identical values, so there may be no way to carve
    out a small high-confidence subset at all. Platt scaling is smooth and
    supports fine-grained thresholds. Since the objective here is *maximum
    autonomy subject to an error budget* rather than minimum ECE, selecting on
    ECE alone would pick the model that cannot be deployed.

    The choice is made on the τ-holdout companies, which neither stage was
    fitted on — never on the test split.
    """
    offenders = [x.eval_lead_id for x in calibration_leads if x.split != "calibration"]
    if offenders:
        raise ValueError(
            f"{len(offenders)} non-calibration leads passed to fit "
            f"(e.g. {offenders[:3]}). The test split must never be used for fitting."
        )

    if method != "auto":
        return _fit_one(
            calibration_leads, target_error_rate, method, delta, tau_holdout_fraction, seed
        )

    candidates = [
        _fit_one(calibration_leads, target_error_rate, m, delta, tau_holdout_fraction, seed)
        for m in ("platt", "isotonic")
    ]
    # Prefer a model that meets the guarantee; among those, the widest
    # coverage; break ties on calibration quality.
    return max(
        candidates,
        key=lambda m: (
            m.threshold.guarantee_met,
            m.threshold.coverage,
            -m.report.ece,
        ),
    )


def _fit_one(
    calibration_leads: Sequence[EvalLead],
    target_error_rate: float,
    method: CalibrationMethod,
    delta: float,
    tau_holdout_fraction: float,
    seed: int,
) -> ConfidenceModel:
    fit_leads, tau_leads = _split_companies(calibration_leads, tau_holdout_fraction)
    if not fit_leads or not tau_leads:
        raise ValueError("calibration split too small to hold out a τ-selection set")

    frame = build_training_frame(fit_leads, seed=seed)

    # Out-of-fold scores: each row is scored by a model that never saw its
    # company. Calibrating on in-sample scores would fit the base model's
    # overconfidence rather than correcting it.
    oof = np.zeros(len(frame), dtype=float)
    for fold in _group_folds(frame.groups, N_CV_FOLDS):
        if len(fold) == 0:
            continue
        mask = np.ones(len(frame), dtype=bool)
        mask[fold] = False
        inner = LogisticRegression(max_iter=1000, solver="lbfgs")
        inner.fit(frame.features[mask], frame.labels[mask])
        oof[fold] = inner.predict_proba(frame.features[fold])[:, 1]

    calibrator = _fit_calibrator(method, oof, frame.labels)

    base = LogisticRegression(max_iter=1000, solver="lbfgs")
    base.fit(frame.features, frame.labels)

    # τ and the reported ECE both come from companies unseen by either stage.
    tau_frame = build_training_frame(tau_leads, seed=seed + 10_000)
    raw = base.predict_proba(tau_frame.features)[:, 1]
    calibrated = np.clip(np.asarray(calibrator.predict(raw), dtype=float), 0.0, 1.0)

    return ConfidenceModel(
        base=base,
        calibrator=calibrator,
        method=method,
        report=calibration_report(calibrated.tolist(), tau_frame.labels.tolist()),
        threshold=select_threshold(
            calibrated.tolist(), tau_frame.labels.tolist(), target_error_rate, delta
        ),
    )
