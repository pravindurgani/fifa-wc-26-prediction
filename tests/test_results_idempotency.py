"""Regression tests for results_2026.json timestamp idempotency.

Mirrors test_live_state_idempotency.py — both files are committed by
the live-matchday.yml "Commit updated JSON" step's git-add allow-list
(predictions.json, predictions_live.json, live_state.json, live_delta.json,
results_2026.json, live_team_state.json, provider_fixture_map.json), so a
timestamp-only diff in any of them triggers a commit + push + Vercel deploy.

Without these guards, every 10-min tick during the 14h match window writes
a bare `updated_at = now()` to results_2026.json. Over the 39-day tournament
that's ~5,600 junk commits + deploys — comfortably over Vercel Hobby's 100/
day cap on busy matchdays.

Tests are split into:
  - pure helper tests (_material_results + _stamp_match_updated_at)
  - end-to-end integration tests that exercise main() against provider=mock
    so the file write path is what's actually validated.
"""
from __future__ import annotations
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _import_module(tmp_path):
    """Re-import fetch_results with LIVE/RAW redirected to tmp_path."""
    sys.modules.pop("scripts.live.fetch_results", None)
    mod = importlib.import_module("scripts.live.fetch_results")
    live = tmp_path / "data" / "live"
    raw = tmp_path / "data" / "raw"
    live.mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)
    mod.LIVE = live
    mod.RAW = raw
    return mod


def _seed_config(mod):
    """Minimal wc2026_config.json so main() doesn't fatal-exit."""
    cfg = {"group_stage_schedule": [
        {"m": 1, "date": "2026-06-11", "home": "Mexico", "away": "Team A"},
        {"m": 2, "date": "2026-06-12", "home": "Team B", "away": "Team C"},
    ]}
    (mod.RAW / "wc2026_config.json").write_text(json.dumps(cfg))


def _seed_results(mod, completed, warnings=None, source="api_football",
                  updated_at="2026-06-10T12:11:03+00:00"):
    payload = {
        "schema": "Completed WC 2026 matches — locked. Future matches are simulated.",
        "updated_at": updated_at,
        "source": source,
        "completed_matches": completed,
        "warnings": warnings or [],
    }
    (mod.LIVE / "results_2026.json").write_text(json.dumps(payload, indent=2))
    return payload


# ─── Pure helper tests ─────────────────────────────────────────────────────

def test_material_helper_ignores_top_level_timestamp(tmp_path):
    mod = _import_module(tmp_path)
    a = {"source": "api_football", "completed_matches": [{"m": 1}], "warnings": [],
         "updated_at": "2026-06-10T12:00:00+00:00"}
    b = {"source": "api_football", "completed_matches": [{"m": 1}], "warnings": [],
         "updated_at": "2026-06-10T18:30:00+00:00"}
    assert mod._material_results(a) == mod._material_results(b)


def test_material_helper_ignores_per_match_timestamp(tmp_path):
    """Inner per-record updated_at must be stripped before comparison —
    fetch_results stamps now() inside each match record on every fetch."""
    mod = _import_module(tmp_path)
    a = {"source": "api_football", "warnings": [], "completed_matches": [
        {"m": 1, "home_score": 2, "away_score": 1, "status": "FT",
         "updated_at": "2026-06-10T12:00:00+00:00"},
    ]}
    b = {"source": "api_football", "warnings": [], "completed_matches": [
        {"m": 1, "home_score": 2, "away_score": 1, "status": "FT",
         "updated_at": "2026-06-10T18:30:00+00:00"},
    ]}
    assert mod._material_results(a) == mod._material_results(b)


def test_material_helper_detects_score_change(tmp_path):
    mod = _import_module(tmp_path)
    a = {"source": "api_football", "warnings": [], "completed_matches": [
        {"m": 1, "home_score": 2, "away_score": 1, "status": "FT"},
    ]}
    b = {"source": "api_football", "warnings": [], "completed_matches": [
        {"m": 1, "home_score": 3, "away_score": 1, "status": "FT"},
    ]}
    assert mod._material_results(a) != mod._material_results(b)


def test_material_helper_detects_new_locked_match(tmp_path):
    mod = _import_module(tmp_path)
    a = {"source": "api_football", "warnings": [], "completed_matches": []}
    b = {"source": "api_football", "warnings": [], "completed_matches": [
        {"m": 1, "home_score": 0, "away_score": 0, "status": "FT"},
    ]}
    assert mod._material_results(a) != mod._material_results(b)


def test_material_helper_detects_warning_change(tmp_path):
    mod = _import_module(tmp_path)
    a = {"source": "api_football", "completed_matches": [], "warnings": []}
    b = {"source": "api_football", "completed_matches": [],
         "warnings": [{"type": "fetch_failure", "message": "x"}]}
    assert mod._material_results(a) != mod._material_results(b)


def test_material_helper_detects_source_change(tmp_path):
    mod = _import_module(tmp_path)
    a = {"source": "api_football", "completed_matches": [], "warnings": []}
    b = {"source": "football_data", "completed_matches": [], "warnings": []}
    assert mod._material_results(a) != mod._material_results(b)


def test_stamp_match_updated_at_preserves_when_unchanged(tmp_path):
    mod = _import_module(tmp_path)
    prior = [{
        "m": 1, "home_score": 2, "away_score": 1, "status": "FT",
        "status_long": "Match Finished", "elapsed": 90, "source": "api_football",
        "raw_status": "FT", "date": "2026-06-11", "home": "Mexico", "away": "South Africa",
        "updated_at": "2026-06-11T19:45:30+00:00",
    }]
    cand = [{
        "m": 1, "home_score": 2, "away_score": 1, "status": "FT",
        "status_long": "Match Finished", "elapsed": 90, "source": "api_football",
        "raw_status": "FT", "date": "2026-06-11", "home": "Mexico", "away": "South Africa",
        "updated_at": "2026-06-11T20:55:30+00:00",  # newer "now()" stamp from fetcher
    }]
    out = mod._stamp_match_updated_at(cand, prior)
    assert out[0]["updated_at"] == prior[0]["updated_at"], \
        "Match material identical but per-record updated_at not reused"


def test_stamp_match_updated_at_bumps_on_score_change(tmp_path):
    mod = _import_module(tmp_path)
    prior = [{
        "m": 1, "home_score": 1, "away_score": 1, "status": "FT",
        "source": "api_football", "raw_status": "FT", "date": "2026-06-11",
        "home": "Mexico", "away": "South Africa",
        "updated_at": "2026-06-11T19:30:00+00:00",
    }]
    cand = [{
        "m": 1, "home_score": 2, "away_score": 1, "status": "FT",  # score changed
        "source": "api_football", "raw_status": "FT", "date": "2026-06-11",
        "home": "Mexico", "away": "South Africa",
        "updated_at": "2026-06-11T19:45:30+00:00",
    }]
    out = mod._stamp_match_updated_at(cand, prior)
    assert out[0]["updated_at"] == cand[0]["updated_at"], \
        "Score changed but per-record updated_at not refreshed"


def test_stamp_match_updated_at_handles_empty_prior(tmp_path):
    mod = _import_module(tmp_path)
    cand = [{"m": 1, "home_score": 2, "away_score": 1, "status": "FT",
             "updated_at": "2026-06-11T19:45:30+00:00"}]
    out = mod._stamp_match_updated_at(cand, None)
    assert out == cand
    out = mod._stamp_match_updated_at(cand, [])
    assert out == cand


# ─── End-to-end integration tests ──────────────────────────────────────────

def test_unchanged_tick_preserves_top_level_timestamp(tmp_path, monkeypatch):
    """Two consecutive main() runs with no provider change → top-level
    `updated_at` must be byte-identical → no git diff."""
    mod = _import_module(tmp_path)
    _seed_config(mod)
    prior_ts = "2026-06-10T12:11:03+00:00"
    locked = [{
        "m": 1, "date": "2026-06-11", "home": "Mexico", "away": "Team A",
        "home_score": 2, "away_score": 1, "status": "FT",
        "status_long": "Match Finished", "elapsed": 90,
        "source": "mock", "raw_status": "FT",
        "updated_at": "2026-06-10T11:00:00+00:00",
    }]
    _seed_results(mod, completed=locked, source="mock", updated_at=prior_ts)

    # provider=mock re-reads the seeded file; the write path is what we're testing
    monkeypatch.setenv("FOOTBALL_PROVIDER", "mock")
    monkeypatch.setattr(sys, "argv", ["fetch_results.py"])
    rc = mod.main()
    assert rc == 0

    after = json.loads((mod.LIVE / "results_2026.json").read_text())
    assert after["updated_at"] == prior_ts, (
        f"Top-level updated_at bumped on no-op tick: "
        f"{prior_ts} → {after['updated_at']}"
    )


def test_new_locked_match_bumps_top_level_timestamp(tmp_path, monkeypatch):
    """When the provider returns a NEW locked match the file didn't have,
    top-level updated_at MUST move.

    Strategy: monkey-patch `fetch_mock` directly so the "provider" can
    diverge from what's on disk (the real fetch_mock just re-reads the
    file, so it can't simulate a fresh lock arriving).
    """
    mod = _import_module(tmp_path)
    _seed_config(mod)
    prior_ts = "2026-06-10T12:11:03+00:00"
    _seed_results(mod, completed=[], source="mock", updated_at=prior_ts)
    new_locked = [{
        "m": 1, "date": "2026-06-11", "home": "Mexico", "away": "Team A",
        "home_score": 2, "away_score": 1, "status": "FT",
        "status_long": "Match Finished", "elapsed": 90,
        "source": "mock", "raw_status": "FT",
        "updated_at": "2026-06-10T19:30:00+00:00",
    }]
    monkeypatch.setattr(mod, "fetch_mock", lambda: new_locked)
    monkeypatch.setenv("FOOTBALL_PROVIDER", "mock")
    monkeypatch.setattr(sys, "argv", ["fetch_results.py"])
    rc = mod.main()
    assert rc == 0
    after = json.loads((mod.LIVE / "results_2026.json").read_text())
    assert after["updated_at"] != prior_ts, \
        "completed_matches grew but top-level updated_at frozen — bump path broken"
    assert len(after["completed_matches"]) == 1


def test_no_prior_file_stamps_fresh(tmp_path, monkeypatch):
    """First-ever run with no prior file must stamp a real timestamp."""
    mod = _import_module(tmp_path)
    _seed_config(mod)
    monkeypatch.setenv("FOOTBALL_PROVIDER", "mock")
    monkeypatch.setattr(sys, "argv", ["fetch_results.py"])
    rc = mod.main()
    assert rc == 0
    p = mod.LIVE / "results_2026.json"
    assert p.exists()
    after = json.loads(p.read_text())
    assert after["updated_at"], "Expected non-empty fresh updated_at"
