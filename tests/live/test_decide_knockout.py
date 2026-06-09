"""
Unit tests for A.3 — decide_knockout() in scripts/03_simulate.py.

Verifies that:
  - Locked knockouts (FT/AET/PEN) short-circuit to the real winner.
  - PEN matches return the winner from the `winner` field, NOT score
    comparison (a 0-0 (3-0 pens) win would otherwise be miscounted).
  - Unlocked matches fall through to the existing Monte Carlo sampler.
  - Missing-winner records fall back to score comparison (defensive).

These tests use a tiny fake matrix + fake rng so we don't depend on the
full simulator pipeline. Pure-function testing of the new decider.

Run:
    python3 tests/live/test_decide_knockout.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

# We need to import decide_knockout. The simulator imports pandas/numpy at
# module-load time; that's fine for the test environment.
import importlib.util
spec = importlib.util.spec_from_file_location(
    "simulate_module", ROOT / "scripts" / "03_simulate.py"
)
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)


# A 1×1 matrix returning [0, 0] every sample — so unlocked paths produce
# deterministic 0-0 → ET → pens output, but ALL locked paths return real
# scores untouched.
_DUMMY_MATRIX = np.array([[1.0]])
_DUMMY_LAMS = (0.5, 0.5)
_DUMMY_ELOS = (1500, 1500)
_DUMMY_CFG = {"lambda_noise_per_match": False, "pen_elo_slope": 200.0}


class TestDecideKnockoutLocked(unittest.TestCase):
    """When a match is in the locked dict, decide_knockout MUST short-circuit."""

    def test_locked_pen_home_wins_via_winner_field(self):
        """Portugal 0-0 (3-0 pens) Slovenia — decoder must read winner field, not score."""
        locked = {
            81: {
                "home_score": 0, "away_score": 0,
                "home_pens": 3, "away_pens": 0,
                "winner": "home", "status": "PEN",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "Portugal", "Slovenia", 81, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual(h, 0)
        self.assertEqual(a, 0)
        self.assertEqual(w, "Portugal",
                         "PEN winner MUST come from `winner` field — "
                         "score comparison would tie and return None")

    def test_locked_pen_away_wins(self):
        """Japan 1-1 (1-3 pens) Croatia — away wins."""
        locked = {
            82: {
                "home_score": 1, "away_score": 1,
                "home_pens": 1, "away_pens": 3,
                "winner": "away", "status": "PEN",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "Japan", "Croatia", 82, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual(w, "Croatia")

    def test_locked_aet_winner_from_field(self):
        """England 2-1 AET Slovakia — winner field decides."""
        locked = {
            73: {
                "home_score": 2, "away_score": 1,
                "home_pens": None, "away_pens": None,
                "winner": "home", "status": "AET",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "England", "Slovakia", 73, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual((h, a, w), (2, 1, "England"))

    def test_locked_ft_winner_from_field(self):
        """Regulation win: Spain 2-1 Brazil → winner field."""
        locked = {
            89: {
                "home_score": 2, "away_score": 1,
                "home_pens": None, "away_pens": None,
                "winner": "home", "status": "FT",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "Spain", "Brazil", 89, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual(w, "Spain")

    def test_locked_missing_winner_falls_back_to_score(self):
        """Defensive: locked record without `winner` falls back to score comparison."""
        locked = {
            73: {
                "home_score": 3, "away_score": 0,
                "home_pens": None, "away_pens": None,
                "winner": None, "status": "FT",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "Germany", "Slovakia", 73, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual(w, "Germany", "score comparison must pick higher score")


class TestDecideKnockoutUnlocked(unittest.TestCase):
    """When a match is NOT in the locked dict, fall through to resolve_knockout."""

    def test_unlocked_falls_through_to_sampler(self):
        """An empty locked dict means every match samples normally."""
        rng = np.random.default_rng(42)
        # Use a real matrix so we get a real sample. Tiny 2x2 favouring 1-0.
        mat = np.array([
            [0.0, 0.0],
            [1.0, 0.0],  # P(home=1, away=0) = 1.0
        ])
        h, a, w = sim.decide_knockout(
            "Spain", "France", 73, {},  # empty locked
            mat, 1.0, 0.5, 1700, 1650, _DUMMY_CFG, rng,
        )
        # Matrix forces home=1 away=0 deterministically → Spain wins.
        self.assertEqual((h, a, w), (1, 0, "Spain"))

    def test_unlocked_match_not_in_dict(self):
        """Locked dict with OTHER matches doesn't affect this one."""
        locked = {
            73: {"home_score": 5, "away_score": 0, "winner": "home", "status": "FT"},
            # M89 NOT locked
        }
        rng = np.random.default_rng(42)
        mat = np.array([[0.0, 1.0], [0.0, 0.0]])  # forces home=0 away=1 → away wins
        h, a, w = sim.decide_knockout(
            "Argentina", "Netherlands", 89, locked,
            mat, 1.0, 1.0, 1700, 1650, _DUMMY_CFG, rng,
        )
        # M89 not in locked → samples → Netherlands (away) per the matrix.
        self.assertEqual(w, "Netherlands")


class TestLoadCompletedMatchesSchema(unittest.TestCase):
    """load_completed_matches captures all the new A.2 fields."""

    def test_captures_pens_and_winner(self):
        """Round-trip: a JSON record with all fields ends up in the dict."""
        import json
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump({
                "completed_matches": [
                    {
                        "m": 81, "home_score": 0, "away_score": 0,
                        "home_pens": 3, "away_pens": 0,
                        "winner": "home", "status": "PEN",
                    },
                    {
                        "m": 1, "home_score": 2, "away_score": 1,
                        "home_pens": None, "away_pens": None,
                        "winner": None, "status": "FT",
                    },
                ]
            }, tmp)
            path = Path(tmp.name)
        try:
            result = sim.load_completed_matches(path)
            self.assertEqual(result[81]["winner"], "home")
            self.assertEqual(result[81]["home_pens"], 3)
            self.assertEqual(result[81]["status"], "PEN")
            self.assertIsNone(result[1]["winner"])
            self.assertIsNone(result[1]["home_pens"])
        finally:
            path.unlink()

    def test_legacy_records_without_new_fields(self):
        """Old results_2026.json (pre-A.2) without pen/winner fields still loads."""
        import json
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump({
                "completed_matches": [
                    {"m": 1, "home_score": 2, "away_score": 1}  # only legacy fields
                ]
            }, tmp)
            path = Path(tmp.name)
        try:
            result = sim.load_completed_matches(path)
            self.assertEqual(result[1]["home_score"], 2)
            self.assertIsNone(result[1].get("winner"),
                              "missing winner field must surface as None, not raise")
        finally:
            path.unlink()


def _summary(result):
    print()
    print(f"  Ran {result.testsRun} tests")
    if result.wasSuccessful():
        print(f"  ✓ all passed")
    else:
        print(f"  ✗ {len(result.failures)} failures, {len(result.errors)} errors")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    _summary(result)
    sys.exit(0 if result.wasSuccessful() else 1)
