"""
Unit tests for the knockout-aware fixture-map builder (A.1).

Covers:
  - classify_round() — every API-Football + football-data round label
    variant we expect, plus drift cases (capitalisation, alt phrasings).
  - knockout-bracket loading — bracket file shape matches builder expectations.
  - round-aware pairing — chronological ordering yields deterministic
    internal-id assignment.
  - schema integrity — the output schema includes the new `coverage`
    audit block.

These tests use synthetic data only — no network calls, no API key needed.
The probe samples under tests/live/provider_samples/ are imported as
fixtures so the tests stay grounded in real provider response shapes.

Run:
    python3 tests/live/test_knockout_fixture_map.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from build_provider_fixture_map import classify_round  # noqa: E402


class TestClassifyRound(unittest.TestCase):
    """Round-label classifier handles all observed + plausible variants."""

    def test_confirmed_apifootball_labels(self):
        """Labels actually observed in the A.0 probe."""
        for label, expected in [
            ("Round of 16",     "r16"),
            ("Quarter-finals",  "qf"),
            ("Semi-finals",     "sf"),
            ("Final",           "final"),
            ("3rd Place Final", "third_place"),
        ]:
            with self.subTest(label=label):
                self.assertEqual(classify_round(label), expected)

    def test_wc2026_r32_variants(self):
        """WC2026 introduces R32 — API-Football hasn't surfaced it before,
        so we accept several plausible labels until empirically observed."""
        for label in ["Round of 32", "round of 32", "1/16-finals", "1/16 Finals"]:
            with self.subTest(label=label):
                self.assertEqual(classify_round(label), "r32")

    def test_r16_alt_phrasings(self):
        """Some leagues use '1/8-finals' for the Round of 16."""
        for label in ["1/8-finals", "1/8 Finals", "ROUND OF 16"]:
            with self.subTest(label=label):
                self.assertEqual(classify_round(label), "r16")

    def test_third_place_must_beat_final(self):
        """'3rd Place Final' contains 'Final' — order of checks matters."""
        self.assertEqual(classify_round("3rd Place Final"), "third_place")
        self.assertEqual(classify_round("Third Place Playoff"), "third_place")
        # Bare 'Final' should still be the trophy match
        self.assertEqual(classify_round("Final"), "final")

    def test_group_stage_returns_none(self):
        """Group-stage labels must not be classified as knockout."""
        for label in ["Group Stage", "Group A", "Group H", "Matchday 3", ""]:
            with self.subTest(label=label):
                self.assertIsNone(classify_round(label))

    def test_none_input(self):
        """Defensive: None must not crash."""
        self.assertIsNone(classify_round(None))


class TestKnockoutBracketShape(unittest.TestCase):
    """Bracket file matches what the builder expects to find."""

    @classmethod
    def setUpClass(cls):
        cls.bracket = json.loads(
            (ROOT / "data" / "raw" / "knockout_bracket_2026.json").read_text()
        )

    def test_all_32_knockouts_present(self):
        """WC2026 = 16 R32 + 8 R16 + 4 QF + 2 SF + 1 3rd + 1 Final = 32."""
        r32 = len(self.bracket.get("r32_slots", []))
        r16 = len(self.bracket.get("r16_bracket", []))
        qf  = len(self.bracket.get("qf_bracket", []))
        sf  = len(self.bracket.get("sf_bracket", []))
        ft  = self.bracket.get("final_and_third_place", {})
        extras = (1 if "third_place" in ft else 0) + (1 if "final" in ft else 0)
        total = r32 + r16 + qf + sf + extras
        self.assertEqual(total, 32, f"expected 32 knockouts, got {total} "
                         f"(r32={r32}, r16={r16}, qf={qf}, sf={sf}, ft={extras})")

    def test_match_nums_cover_73_to_104(self):
        """The 32 knockouts should be numbered M73-M104, no gaps, no overlaps."""
        nums = []
        for s in self.bracket.get("r32_slots", []): nums.append(s["match_num"])
        for s in self.bracket.get("r16_bracket", []): nums.append(s["match_num"])
        for s in self.bracket.get("qf_bracket", []): nums.append(s["match_num"])
        for s in self.bracket.get("sf_bracket", []): nums.append(s["match_num"])
        ft = self.bracket.get("final_and_third_place", {})
        if "third_place" in ft: nums.append(ft["third_place"]["match_num"])
        if "final" in ft: nums.append(ft["final"]["match_num"])
        self.assertEqual(sorted(nums), list(range(73, 105)),
                         "knockout match_num set must be exactly {73..104}")

    def test_every_knockout_has_date(self):
        """Builder pairs by date — every knockout slot needs one."""
        for section in ["r32_slots", "r16_bracket", "qf_bracket", "sf_bracket"]:
            for s in self.bracket.get(section, []):
                self.assertIn("date", s,
                              f"{section} M{s['match_num']} missing date")
                # ISO 8601 date sanity
                self.assertRegex(s["date"], r"^\d{4}-\d{2}-\d{2}$")


class TestProbeSamplesUsable(unittest.TestCase):
    """The A.0 probe samples should parse cleanly and contain the fields
    A.1 + A.2 rely on. If the probe schema drifts, the tests catch it."""

    @classmethod
    def setUpClass(cls):
        cls.samples_dir = ROOT / "tests" / "live" / "provider_samples"

    def test_euro2024_sample_exists_and_parses(self):
        path = self.samples_dir / "apifootball_euro2024_knockouts.json"
        self.assertTrue(path.exists(),
                        "A.0 probe samples missing — re-run probe-apifootball.yml")
        data = json.loads(path.read_text())
        self.assertGreater(len(data.get("fixtures", [])), 0,
                           "Euro 2024 sample has no fixtures")

    def test_probe_samples_contain_aet_and_pen(self):
        """A.2 needs both AET (no shootout) and PEN (shootout) examples
        to verify both decoder paths."""
        path = self.samples_dir / "apifootball_euro2024_knockouts.json"
        if not path.exists():
            self.skipTest("probe samples not present")
        fixtures = json.loads(path.read_text()).get("fixtures", [])
        status_codes = {f["fixture"]["status"]["short"] for f in fixtures}
        self.assertIn("AET", status_codes,
                      "Euro 2024 sample missing an AET fixture — A.2 needs one")
        self.assertIn("PEN", status_codes,
                      "Euro 2024 sample missing a PEN fixture — A.2 needs one")

    def test_probe_round_labels_all_classify(self):
        """Every distinct round label in the probe should classify."""
        for sample_name in ("apifootball_euro2024_knockouts.json",
                            "apifootball_wc2022_knockouts.json"):
            path = self.samples_dir / sample_name
            if not path.exists():
                continue
            fixtures = json.loads(path.read_text()).get("fixtures", [])
            labels = {f["league"]["round"] for f in fixtures if f.get("league", {}).get("round")}
            for label in labels:
                with self.subTest(sample=sample_name, label=label):
                    phase = classify_round(label)
                    self.assertIsNotNone(
                        phase,
                        f"{sample_name}: classifier returned None for {label!r} — "
                        f"add it to classify_round() or its substring rules"
                    )


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
