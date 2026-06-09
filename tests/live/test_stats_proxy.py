"""
Unit tests for B.5 — stats_proxy_adjustments + fetch_match_stats normalisation.

No network; synthetic /fixtures/statistics responses.

Run:
    python3 tests/live/test_stats_proxy.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from stats_proxy_adjustments import (  # noqa: E402
    stats_to_dict, compute_form_delta, both_form_deltas,
    STATS_PROXY_RAW_CAP,
)
import fetch_match_stats  # noqa: E402


class TestStatsToDict(unittest.TestCase):
    def test_parses_ints_and_percent_strings(self):
        d = stats_to_dict([
            {"type": "Shots on Goal", "value": 6},
            {"type": "Ball Possession", "value": "57%"},
            {"type": "Corner Kicks", "value": "4"},
            {"type": "Empty Field", "value": None},
        ])
        self.assertEqual(d["Shots on Goal"], 6)
        self.assertEqual(d["Ball Possession"], 57)
        self.assertEqual(d["Corner Kicks"], 4)
        self.assertIsNone(d["Empty Field"])

    def test_empty_list(self):
        self.assertEqual(stats_to_dict([]), {})

    def test_none_safe(self):
        self.assertEqual(stats_to_dict(None), {})


class TestComputeFormDelta(unittest.TestCase):
    def test_dominant_team_positive_delta(self):
        own = {"Shots on Goal": 8, "Ball Possession": 65, "Corner Kicks": 7}
        opp = {"Shots on Goal": 2, "Ball Possession": 35, "Corner Kicks": 2}
        delta = compute_form_delta(own, opp)
        self.assertGreater(delta, 0)
        # Should be sizeable but capped
        self.assertLessEqual(delta, STATS_PROXY_RAW_CAP)

    def test_dominated_team_negative_delta(self):
        own = {"Shots on Goal": 2, "Ball Possession": 35, "Corner Kicks": 2}
        opp = {"Shots on Goal": 8, "Ball Possession": 65, "Corner Kicks": 7}
        delta = compute_form_delta(own, opp)
        self.assertLess(delta, 0)
        self.assertGreaterEqual(delta, -STATS_PROXY_RAW_CAP)

    def test_balanced_match_near_zero(self):
        own = {"Shots on Goal": 4, "Ball Possession": 50, "Corner Kicks": 5}
        opp = {"Shots on Goal": 4, "Ball Possession": 50, "Corner Kicks": 5}
        self.assertEqual(compute_form_delta(own, opp), 0.0)

    def test_missing_possession_safe(self):
        own = {"Shots on Goal": 5, "Corner Kicks": 3}
        opp = {"Shots on Goal": 3, "Corner Kicks": 2}
        delta = compute_form_delta(own, opp)
        self.assertGreater(delta, 0)  # shot edge still scores

    def test_raw_cap_enforced(self):
        own = {"Shots on Goal": 100, "Ball Possession": 99, "Corner Kicks": 50}
        opp = {"Shots on Goal": 0, "Ball Possession": 1, "Corner Kicks": 0}
        delta = compute_form_delta(own, opp)
        self.assertEqual(delta, STATS_PROXY_RAW_CAP)

    def test_both_form_deltas_are_signed_pair(self):
        home = [{"type": "Shots on Goal", "value": 7},
                {"type": "Ball Possession", "value": "60%"},
                {"type": "Corner Kicks", "value": 6}]
        away = [{"type": "Shots on Goal", "value": 3},
                {"type": "Ball Possession", "value": "40%"},
                {"type": "Corner Kicks", "value": 3}]
        h_d, a_d = both_form_deltas(home, away)
        self.assertGreater(h_d, 0)
        self.assertLess(a_d, 0)
        # By symmetry they should be exact negatives of each other
        self.assertAlmostEqual(h_d, -a_d, places=4)


class TestBuildMatchEntry(unittest.TestCase):
    def test_schema_and_xg_flag_locked_false(self):
        match = {"m": 1, "home": "Mexico", "away": "South Africa", "status": "FT"}
        response = [
            {"team": {"name": "Mexico"},
             "statistics": [{"type": "Shots on Goal", "value": 6},
                            {"type": "Ball Possession", "value": "55%"},
                            {"type": "Corner Kicks", "value": 5}]},
            {"team": {"name": "South Africa"},
             "statistics": [{"type": "Shots on Goal", "value": 2},
                            {"type": "Ball Possession", "value": "45%"},
                            {"type": "Corner Kicks", "value": 2}]},
        ]
        entry = fetch_match_stats.build_match_entry(match, response, "1489369")
        self.assertEqual(entry["match_id"], 1)
        self.assertEqual(entry["status"], "FT")
        self.assertFalse(entry["true_xg_available"],
                         "true_xg_available must be False — spec lock")
        self.assertGreater(entry["home_form_adjustment_elo"], 0)
        self.assertLess(entry["away_form_adjustment_elo"], 0)
        self.assertEqual(entry["fixture_id"], "1489369")
        self.assertIn("Shots on Goal", entry["home_stats"])

    def test_handles_missing_team_block(self):
        """If the stats endpoint returns one side only (rare), don't crash."""
        match = {"m": 1, "home": "A", "away": "B", "status": "FT"}
        response = [{"team": {"name": "A"},
                     "statistics": [{"type": "Shots on Goal", "value": 5}]}]
        entry = fetch_match_stats.build_match_entry(match, response, "1")
        self.assertIsNotNone(entry["home_form_adjustment_elo"])
        self.assertIsNotNone(entry["away_form_adjustment_elo"])


class TestBuildSnapshot(unittest.TestCase):
    def test_snapshot_schema_and_notes(self):
        snap = fetch_match_stats.build_snapshot([], [])
        self.assertEqual(snap["schema_version"], 1)
        self.assertEqual(snap["source"], "api_football")
        self.assertIn("PROXY", snap["notes"])
        self.assertIn("NOT xG", snap["notes"])
        self.assertEqual(snap["n_completed"], 0)


def _summary(result):
    print()
    print(f"  Ran {result.testsRun} tests")
    if result.wasSuccessful():
        print("  ✓ all passed")
    else:
        print(f"  ✗ {len(result.failures)} failures, {len(result.errors)} errors")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    _summary(result)
    sys.exit(0 if result.wasSuccessful() else 1)
