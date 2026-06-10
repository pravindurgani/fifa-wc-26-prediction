"""
Tests for dashboard/predictions.json — strongest invariants from 09_validate.py
ported into pytest so they run on every push, not only pre-deploy.

Each invariant is a separate test function so failures point at the exact rule.

Run from repo root:
    python -m pytest tests/test_predictions_schema.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import pytest
    HAS_PYTEST = True
except ImportError:  # pragma: no cover
    HAS_PYTEST = False

    class _PytestStub:
        @staticmethod
        def fixture(*_a, **_kw):
            def deco(fn):
                return fn
            return deco

        @staticmethod
        def skip(msg):
            print(f"  [SKIP] {msg}")

    pytest = _PytestStub()  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
PRED_PATH = ROOT / "dashboard" / "predictions.json"

EPS = 1e-3


def _load_predictions() -> dict:
    if not PRED_PATH.exists():
        if HAS_PYTEST:
            pytest.skip(f"predictions.json missing at {PRED_PATH}")
        return {}
    return json.loads(PRED_PATH.read_text())


# ---------- shape ----------------------------------------------------------
def test_predictions_file_exists_and_parses():
    assert PRED_PATH.exists(), f"predictions.json missing at {PRED_PATH}"
    data = json.loads(PRED_PATH.read_text())
    assert "team_predictions" in data
    assert "match_predictions" in data


def test_has_48_team_predictions():
    data = _load_predictions()
    teams = data["team_predictions"]
    assert len(teams) == 48, f"expected 48 teams, got {len(teams)}"


def test_has_72_group_match_predictions():
    data = _load_predictions()
    matches = data["match_predictions"]
    assert len(matches) == 72, f"expected 72 group matches, got {len(matches)}"


# ---------- probability invariants -----------------------------------------
def test_p_champion_sums_to_1():
    data = _load_predictions()
    s = sum(t["p_champion"] for t in data["team_predictions"])
    assert abs(s - 1.0) < EPS, f"Σ p_champion = {s:.6f}, expected ~1.0"


def test_p_reach_final_sums_to_2():
    data = _load_predictions()
    s = sum(t["p_reach_final"] for t in data["team_predictions"])
    assert abs(s - 2.0) < EPS, f"Σ p_reach_final = {s:.6f}, expected ~2.0"


def test_p_reach_sf_sums_to_4():
    data = _load_predictions()
    s = sum(t["p_reach_sf"] for t in data["team_predictions"])
    assert abs(s - 4.0) < EPS, f"Σ p_reach_sf = {s:.6f}, expected ~4.0"


def test_p_reach_qf_sums_to_8():
    data = _load_predictions()
    s = sum(t["p_reach_qf"] for t in data["team_predictions"])
    assert abs(s - 8.0) < EPS, f"Σ p_reach_qf = {s:.6f}, expected ~8.0"


# ---------- monotonicity ---------------------------------------------------
def test_per_team_round_monotonicity():
    """champion ≤ final ≤ SF ≤ QF ≤ advance_groups for every team."""
    data = _load_predictions()
    violations = []
    for t in data["team_predictions"]:
        seq = [
            ("p_champion", t["p_champion"]),
            ("p_reach_final", t["p_reach_final"]),
            ("p_reach_sf", t["p_reach_sf"]),
            ("p_reach_qf", t["p_reach_qf"]),
            ("p_advance_groups", t["p_advance_groups"]),
        ]
        for (n1, v1), (n2, v2) in zip(seq, seq[1:]):
            # Allow tiny MC noise tolerance.
            if v1 > v2 + EPS:
                violations.append(f"{t['team']}: {n1}={v1:.4f} > {n2}={v2:.4f}")
    assert not violations, "monotonicity violated:\n  " + "\n  ".join(violations[:5])


# ---------- CI bounds ------------------------------------------------------
def test_per_team_champion_ci_bounds():
    """p_champion_p05 ≤ p_champion ≤ p_champion_p95 for every team."""
    data = _load_predictions()
    violations = []
    for t in data["team_predictions"]:
        p = t["p_champion"]
        lo = t["p_champion_p05"]
        hi = t["p_champion_p95"]
        if lo > p + EPS or p > hi + EPS:
            violations.append(f"{t['team']}: p05={lo:.4f} ≤ p={p:.4f} ≤ p95={hi:.4f} violated")
    assert not violations, "CI bounds violated:\n  " + "\n  ".join(violations[:5])


# ---------- group finish positions sum -------------------------------------
def test_per_team_finish_positions_sum_to_1():
    """p_finish_1st + 2nd + 3rd + 4th == 1.0 ± 1e-3 for every team."""
    data = _load_predictions()
    violations = []
    for t in data["team_predictions"]:
        s = (t["p_finish_1st_group"] + t["p_finish_2nd_group"]
             + t["p_finish_3rd_group"] + t["p_finish_4th_group"])
        if abs(s - 1.0) > EPS:
            violations.append(f"{t['team']}: Σ finish = {s:.6f}")
    assert not violations, (
        "finish-position sum violated:\n  " + "\n  ".join(violations[:5])
    )


# ---------- annex C --------------------------------------------------------
def test_annex_c_misses_is_zero():
    data = _load_predictions()
    misses = data.get("annex_c_misses", -1)
    assert misses == 0, f"annex_c_misses = {misses}, must be 0"


if __name__ == "__main__":
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  [FAIL] {fn.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"  [ERR ] {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n  {len(fns) - failures}/{len(fns)} tests passed")
    sys.exit(0 if failures == 0 else 1)
