"""Regression tests for live_state.json timestamp idempotency.

The live workflow tick rate (~10 min) × tournament window (~30 days) means
~4,000 ticks. Without idempotency, each tick rewrites last_updated_utc and
produces a commit + Vercel deploy. Vercel Hobby caps at 100 deploys/day —
we'd serve stale data evenings of marquee matchdays. This test guards the
no-op-tick behavior.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _import_module(tmp_path, dash_dir):
    """Re-import run_live_update with DASH redirected to tmp_path."""
    # Use importlib so we get a fresh module bound to a redirected DASH.
    import importlib
    sys.modules.pop("scripts.live.run_live_update", None)
    mod = importlib.import_module("scripts.live.run_live_update")
    mod.DASH = dash_dir
    return mod


def test_unchanged_state_preserves_timestamp(tmp_path):
    dash = tmp_path / "dashboard"
    dash.mkdir()
    mod = _import_module(tmp_path, dash)
    # Seed prior state with a fixed timestamp
    prior = {
        "mode": "pre_tournament",
        "last_updated_utc": "2026-06-10T12:11:03+00:00",
        "completed_matches_count": 0,
        "simulation_rerun_this_tick": False,
        "source": "api_football",
        "provider_mode": "active",
        "warnings": [],
    }
    (dash / "live_state.json").write_text(json.dumps(prior, indent=2))

    state = mod.write_live_state(
        mode="pre_tournament",
        completed_count=0,
        sim_rerun=False,
        warnings=[],
        source="api_football",
        provider_mode="active",
    )
    assert state["last_updated_utc"] == prior["last_updated_utc"]
    written = json.loads((dash / "live_state.json").read_text())
    assert written["last_updated_utc"] == prior["last_updated_utc"]


def test_completed_count_change_bumps_timestamp(tmp_path):
    dash = tmp_path / "dashboard"
    dash.mkdir()
    mod = _import_module(tmp_path, dash)
    prior = {
        "mode": "pre_tournament",
        "last_updated_utc": "2026-06-10T12:11:03+00:00",
        "completed_matches_count": 0,
        "simulation_rerun_this_tick": False,
        "source": "api_football",
        "provider_mode": "active",
        "warnings": [],
    }
    (dash / "live_state.json").write_text(json.dumps(prior, indent=2))

    state = mod.write_live_state(
        mode="live",
        completed_count=1,
        sim_rerun=True,
        warnings=[],
        source="api_football",
        provider_mode="active",
    )
    assert state["last_updated_utc"] != prior["last_updated_utc"]


def test_warning_change_bumps_timestamp(tmp_path):
    dash = tmp_path / "dashboard"
    dash.mkdir()
    mod = _import_module(tmp_path, dash)
    prior = {
        "mode": "pre_tournament",
        "last_updated_utc": "2026-06-10T12:11:03+00:00",
        "completed_matches_count": 0,
        "simulation_rerun_this_tick": False,
        "source": "api_football",
        "provider_mode": "active",
        "warnings": [],
    }
    (dash / "live_state.json").write_text(json.dumps(prior, indent=2))

    state = mod.write_live_state(
        mode="pre_tournament",
        completed_count=0,
        sim_rerun=False,
        warnings=[{"type": "fetch_failure", "message": "timeout"}],
        source="api_football",
        provider_mode="active",
    )
    assert state["last_updated_utc"] != prior["last_updated_utc"]


def test_no_prior_file_stamps_fresh(tmp_path):
    dash = tmp_path / "dashboard"
    dash.mkdir()
    mod = _import_module(tmp_path, dash)
    state = mod.write_live_state(
        mode="pre_tournament",
        completed_count=0,
        sim_rerun=False,
        warnings=[],
        source="api_football",
        provider_mode="active",
    )
    assert state["last_updated_utc"]  # non-empty


def test_corrupt_prior_file_stamps_fresh(tmp_path):
    dash = tmp_path / "dashboard"
    dash.mkdir()
    mod = _import_module(tmp_path, dash)
    (dash / "live_state.json").write_text("{not valid json")
    state = mod.write_live_state(
        mode="pre_tournament",
        completed_count=0,
        sim_rerun=False,
        warnings=[],
        source="api_football",
        provider_mode="active",
    )
    assert state["last_updated_utc"]  # non-empty, fresh stamp
