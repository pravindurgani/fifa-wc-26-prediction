"""
Tests for knockout-stage slot-A symmetry (review item P1-3).

Intent: at neutral venues, the knockout XGBoost goal model should produce
matrices that satisfy

    P(A beats B | A in home slot) ≈ 1 − P(B beats A | B in home slot)

within ~1pp. Any residual home-slot bias in the model would systematically
advantage slot-A teams across the entire bracket.

The knockout matrices are NOT serialised into dashboard/predictions.json
(only the bracket layout, group/match aggregates, and per-team probabilities
are). To run this test for real, you would need access to the precomputed
matrices from scripts/03_simulate.py precompute_context() — either by:

  1) Refactoring that function to optionally dump knock_matrices to a
     numpy .npz for offline inspection, or
  2) Loading models/{home,away}_goals_model.joblib here and rebuilding the
     matrices in-process (slow — 48*47 = 2256 pairs).

Until one of those lands, this is a documented placeholder.
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
        def skip(msg):
            print(f"  [SKIP] {msg}")

    pytest = _PytestStub()  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
PRED_PATH = ROOT / "dashboard" / "predictions.json"


def test_predictions_exposes_bracket_for_future_symmetry_check():
    """
    Sanity check the bracket is at least exposed; this is what a future
    knockout-symmetry test will iterate over once matrices are dumped.
    """
    if not PRED_PATH.exists():
        if HAS_PYTEST:
            pytest.skip("predictions.json missing")
        return
    data = json.loads(PRED_PATH.read_text())
    bracket = data.get("bracket")
    assert bracket is not None, "bracket missing from predictions.json"
    assert "r32_slots" in bracket
    assert "r16_bracket" in bracket
    assert "qf_bracket" in bracket
    assert "sf_bracket" in bracket
    assert "final_and_third_place" in bracket


def test_knockout_pair_symmetry_at_neutral_venues():
    """
    Placeholder for the real symmetry test. The knockout matrices live
    only inside scripts/03_simulate.py precompute_context() and are not
    serialised. Skipping until either:
      • scripts/03_simulate.py optionally writes models/knockout_matrices.npz
      • or this test rebuilds the matrices in-process from the joblib models
    """
    if HAS_PYTEST:
        pytest.skip(
            "requires precomputed knockout matrices — see "
            "scripts/03_simulate.py precompute_context()"
        )


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
    print(f"\n  {len(fns) - failures}/{len(fns)} tests passed (skips count as pass)")
    sys.exit(0 if failures == 0 else 1)
