"""
Unit tests for A.2 — knockout decoder (PEN sub-scores + winner) in
scripts/live/fetch_results.py.

Each test is grounded in a real fixture from the A.0 probe samples
(tests/live/provider_samples/apifootball_*.json) so the test suite catches
schema drift the day API-Football changes a field.

Run:
    python3 tests/live/test_knockout_extraction.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from fetch_results import extract_pens_and_winner  # noqa: E402

SAMPLES_DIR = ROOT / "tests" / "live" / "provider_samples"


def _find_sample(path: Path, predicate):
    """Pick the first fixture from a probe sample that matches a predicate."""
    fixtures = json.loads(path.read_text()).get("fixtures", [])
    for f in fixtures:
        if predicate(f):
            return f
    raise AssertionError(f"no matching fixture found in {path.name}")


class TestExtractPensAndWinnerAPIFootball(unittest.TestCase):
    """Grounded in real Euro 2024 + WC 2022 probe samples."""

    @classmethod
    def setUpClass(cls):
        cls.euro = SAMPLES_DIR / "apifootball_euro2024_knockouts.json"
        cls.wc22 = SAMPLES_DIR / "apifootball_wc2022_knockouts.json"
        if not cls.euro.exists() or not cls.wc22.exists():
            raise unittest.SkipTest("A.0 probe samples missing — re-run probe-apifootball.yml")

    # ── PEN: real shootout ──────────────────────────────────────────────
    def test_pen_portugal_slovenia_shootout(self):
        """Euro 2024 R16: Portugal 0-0 (3-0 pens) Slovenia. Portugal wins."""
        f = _find_sample(
            self.euro,
            lambda f: (f["fixture"]["status"]["short"] == "PEN"
                       and f["teams"]["home"]["name"] == "Portugal"),
        )
        home_pens, away_pens, winner = extract_pens_and_winner(
            f["score"], f["teams"], "PEN", winner_source="api_football",
        )
        self.assertEqual(home_pens, 3)
        self.assertEqual(away_pens, 0)
        self.assertEqual(winner, "home")

    def test_pen_japan_croatia_away_win(self):
        """WC 2022 R16: Japan 1-1 (1-3 pens) Croatia. Croatia (away) wins."""
        f = _find_sample(
            self.wc22,
            lambda f: (f["fixture"]["status"]["short"] == "PEN"
                       and f["teams"]["home"]["name"] == "Japan"),
        )
        home_pens, away_pens, winner = extract_pens_and_winner(
            f["score"], f["teams"], "PEN", winner_source="api_football",
        )
        self.assertEqual(home_pens, 1)
        self.assertEqual(away_pens, 3)
        self.assertEqual(winner, "away")

    # ── AET: extra time decided, no shootout ──────────────────────────
    def test_aet_england_slovakia_extra_time(self):
        """Euro 2024 R16: England 2-1 AET Slovakia. England wins via ET goal."""
        f = _find_sample(
            self.euro,
            lambda f: (f["fixture"]["status"]["short"] == "AET"
                       and f["teams"]["home"]["name"] == "England"),
        )
        home_pens, away_pens, winner = extract_pens_and_winner(
            f["score"], f["teams"], "AET", winner_source="api_football",
        )
        self.assertIsNone(home_pens, "AET match must NOT populate pen sub-scores")
        self.assertIsNone(away_pens, "AET match must NOT populate pen sub-scores")
        self.assertEqual(winner, "home")

    # ── FT: regulation win ─────────────────────────────────────────────
    def test_ft_match_winner_extracted(self):
        """Any FT knockout match still extracts the winner."""
        # The Euro 2024 Final was Spain 2-1 England (FT, not AET — added by ET? no, regulation).
        # Take any FT fixture.
        f = _find_sample(
            self.euro,
            lambda f: f["fixture"]["status"]["short"] == "FT",
        )
        home_pens, away_pens, winner = extract_pens_and_winner(
            f["score"], f["teams"], "FT", winner_source="api_football",
        )
        self.assertIsNone(home_pens, "FT match must NOT populate pen sub-scores")
        self.assertIsNone(away_pens)
        self.assertIn(winner, ("home", "away"),
                      "completed knockout FT must have a winner")


class TestExtractPensAndWinnerEdgeCases(unittest.TestCase):
    """Synthetic edge cases: provider hiccups, missing fields, draws."""

    def test_pen_with_missing_winner_returns_none(self):
        """Provider classified as PEN but both winner fields null — caller skips."""
        score = {"penalty": {"home": 4, "away": 3}}
        teams = {"home": {"name": "A", "winner": None}, "away": {"name": "B", "winner": None}}
        home_pens, away_pens, winner = extract_pens_and_winner(
            score, teams, "PEN", winner_source="api_football",
        )
        self.assertEqual(home_pens, 4)
        self.assertEqual(away_pens, 3)
        self.assertIsNone(winner, "missing winner must surface as None — never fabricate")

    def test_pen_with_both_winners_false_returns_none(self):
        """Defensive: both winner=false is the same as both null."""
        score = {"penalty": {"home": 2, "away": 4}}
        teams = {"home": {"winner": False}, "away": {"winner": False}}
        _, _, winner = extract_pens_and_winner(score, teams, "PEN", winner_source="api_football")
        self.assertIsNone(winner)

    def test_string_pens_coerced_to_int(self):
        """Some providers serialise pens as strings — coerce defensively."""
        score = {"penalty": {"home": "5", "away": "3"}}
        teams = {"home": {"winner": True}, "away": {"winner": False}}
        home_pens, away_pens, _ = extract_pens_and_winner(
            score, teams, "PEN", winner_source="api_football",
        )
        self.assertEqual(home_pens, 5)
        self.assertEqual(away_pens, 3)

    def test_bogus_pens_coerced_to_none(self):
        """Defensive: unparseable pen values become None, not exceptions."""
        score = {"penalty": {"home": "weird", "away": None}}
        teams = {"home": {"winner": True}, "away": {"winner": False}}
        home_pens, away_pens, winner = extract_pens_and_winner(
            score, teams, "PEN", winner_source="api_football",
        )
        self.assertIsNone(home_pens)
        self.assertIsNone(away_pens)
        self.assertEqual(winner, "home")

    def test_group_stage_no_penalty_block(self):
        """Group fixtures have penalty={null,null}, no winner — returns all None."""
        score = {"penalty": {"home": None, "away": None}}
        teams = {"home": {"winner": True}, "away": {"winner": False}}
        home_pens, away_pens, winner = extract_pens_and_winner(
            score, teams, "FT", winner_source="api_football",
        )
        self.assertIsNone(home_pens)
        self.assertIsNone(away_pens)
        self.assertEqual(winner, "home", "even group FT wins still surface a winner if provider says so")

    def test_group_stage_draw_no_winner(self):
        """Group draws: winner is None on both sides → None."""
        score = {"penalty": {"home": None, "away": None}}
        teams = {"home": {"winner": None}, "away": {"winner": None}}
        _, _, winner = extract_pens_and_winner(score, teams, "FT", winner_source="api_football")
        self.assertIsNone(winner)

    def test_empty_inputs_defensive(self):
        """Defensive: empty dicts don't crash."""
        home_pens, away_pens, winner = extract_pens_and_winner(
            {}, {}, "FT", winner_source="api_football",
        )
        self.assertIsNone(home_pens)
        self.assertIsNone(away_pens)
        self.assertIsNone(winner)


class TestExtractPensAndWinnerFootballData(unittest.TestCase):
    """football-data.org uses parallel field names; same helper handles both."""

    def test_fd_pen_home_wins(self):
        score = {
            "penalties": {"home": 4, "away": 3},
            "winner": "HOME_TEAM",
        }
        home_pens, away_pens, winner = extract_pens_and_winner(
            score, {}, "PEN", winner_source="football_data",
        )
        self.assertEqual(home_pens, 4)
        self.assertEqual(away_pens, 3)
        self.assertEqual(winner, "home")

    def test_fd_pen_away_wins(self):
        score = {"penalties": {"home": 2, "away": 4}, "winner": "AWAY_TEAM"}
        _, _, winner = extract_pens_and_winner(score, {}, "PEN", winner_source="football_data")
        self.assertEqual(winner, "away")

    def test_fd_draw_no_winner(self):
        score = {"penalties": {"home": None, "away": None}, "winner": "DRAW"}
        _, _, winner = extract_pens_and_winner(score, {}, "FT", winner_source="football_data")
        self.assertIsNone(winner, "DRAW must NOT resolve to either side")

    def test_fd_missing_winner_field(self):
        score = {"penalties": {"home": 3, "away": 1}}
        _, _, winner = extract_pens_and_winner(score, {}, "PEN", winner_source="football_data")
        self.assertIsNone(winner)


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
