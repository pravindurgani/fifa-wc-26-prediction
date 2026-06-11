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

import pytest

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


def test_synced_signature_is_persisted_on_explicit_pass(tmp_path):
    """When a sim runs, the orchestrator passes the current results
    signature so the next tick can detect score corrections."""
    dash = tmp_path / "dashboard"
    dash.mkdir()
    mod = _import_module(tmp_path, dash)
    state = mod.write_live_state(
        mode="live", completed_count=1, sim_rerun=True, warnings=[],
        source="api_football", provider_mode="active",
        synced_signature="abc123def456",
    )
    assert state["synced_signature"] == "abc123def456"
    written = json.loads((dash / "live_state.json").read_text())
    assert written["synced_signature"] == "abc123def456"


def test_synced_signature_is_preserved_when_omitted(tmp_path):
    """No-op ticks (early-exit path) don't pass a signature — the prior
    value must be retained so the last-good marker doesn't get reset to
    empty between sims."""
    dash = tmp_path / "dashboard"
    dash.mkdir()
    mod = _import_module(tmp_path, dash)
    prior = {
        "mode": "live",
        "last_updated_utc": "2026-06-15T12:00:00+00:00",
        "completed_matches_count": 1,
        "simulation_rerun_this_tick": True,
        "source": "api_football",
        "provider_mode": "active",
        "warnings": [],
        "synced_signature": "abc123def456",
    }
    (dash / "live_state.json").write_text(json.dumps(prior))
    # Subsequent no-op tick — call without synced_signature.
    state = mod.write_live_state(
        mode="live", completed_count=1, sim_rerun=False, warnings=[],
        source="api_football", provider_mode="active",
    )
    assert state["synced_signature"] == "abc123def456"  # preserved


def test_signature_change_bumps_timestamp(tmp_path):
    """Same count + same mode but different synced_signature must bump
    last_updated_utc — that's the timestamp signal for score corrections."""
    dash = tmp_path / "dashboard"
    dash.mkdir()
    mod = _import_module(tmp_path, dash)
    prior = {
        "mode": "live",
        "last_updated_utc": "2026-06-15T12:00:00+00:00",
        "completed_matches_count": 1,
        "simulation_rerun_this_tick": True,
        "source": "api_football",
        "provider_mode": "active",
        "warnings": [],
        "synced_signature": "old_sig_1234",
    }
    (dash / "live_state.json").write_text(json.dumps(prior))
    state = mod.write_live_state(
        mode="live", completed_count=1, sim_rerun=True, warnings=[],
        source="api_football", provider_mode="active",
        synced_signature="new_sig_5678",
    )
    assert state["last_updated_utc"] != prior["last_updated_utc"]
    assert state["synced_signature"] == "new_sig_5678"


def test_source_preserved_from_prior_when_omitted(tmp_path):
    """Circuit-breaker / crash paths can't compute a truthful source.
    write_live_state must preserve the prior file's source rather than
    silently reverting to env-var detection (which lies on downgrade)."""
    dash = tmp_path / "dashboard"
    dash.mkdir()
    mod = _import_module(tmp_path, dash)
    prior = {
        "mode": "live",
        "last_updated_utc": "2026-06-15T12:00:00+00:00",
        "completed_matches_count": 1,
        "simulation_rerun_this_tick": True,
        "source": "mock",
        "provider_mode": "manual",
        "warnings": [],
        "synced_signature": "abc",
    }
    (dash / "live_state.json").write_text(json.dumps(prior))
    state = mod.write_live_state(
        mode="live", completed_count=1, sim_rerun=False,
        warnings=[{"type": "circuit_breaker", "message": "tripped"}],
    )
    assert state["source"] == "mock"
    assert state["provider_mode"] == "manual"


def test_completed_signature_canonicalises_order_and_ignores_updated_at(tmp_path, monkeypatch):
    """get_completed_signature() must be stable across no-op ticks: the
    per-record `updated_at` is volatile, and list order can vary. Two
    payloads that differ only on those volatile fields must hash equal."""
    live = tmp_path / "live"
    live.mkdir()
    mod = _import_module(tmp_path, tmp_path / "dashboard")
    monkeypatch.setattr(mod, "LIVE", live)
    base = [
        {"m": 4, "status": "FT", "home_goals": 2, "away_goals": 1, "updated_at": "T1"},
        {"m": 7, "status": "FT", "home_goals": 0, "away_goals": 3, "updated_at": "T1"},
    ]
    (live / "results_2026.json").write_text(json.dumps(
        {"source": "api_football", "completed_matches": base}))
    sig_a = mod.get_completed_signature()
    # Swap order + bump per-record updated_at — must still hash equal.
    permuted = [
        {"m": 7, "status": "FT", "home_goals": 0, "away_goals": 3, "updated_at": "T2"},
        {"m": 4, "status": "FT", "home_goals": 2, "away_goals": 1, "updated_at": "T2"},
    ]
    (live / "results_2026.json").write_text(json.dumps(
        {"source": "api_football", "completed_matches": permuted}))
    sig_b = mod.get_completed_signature()
    assert sig_a == sig_b
    # Change a score — must differ. This is the score-correction case.
    corrected = [
        {"m": 4, "status": "FT", "home_goals": 2, "away_goals": 0, "updated_at": "T2"},
        {"m": 7, "status": "FT", "home_goals": 0, "away_goals": 3, "updated_at": "T2"},
    ]
    (live / "results_2026.json").write_text(json.dumps(
        {"source": "api_football", "completed_matches": corrected}))
    sig_c = mod.get_completed_signature()
    assert sig_c != sig_a


def test_results_source_helper_reads_truthful_source(tmp_path, monkeypatch):
    """get_results_source() must return what fetch_results.py actually
    wrote — not what env vars imply. This is the truth-source for the
    downgrade-warning logic in main()."""
    live = tmp_path / "live"
    live.mkdir()
    mod = _import_module(tmp_path, tmp_path / "dashboard")
    monkeypatch.setattr(mod, "LIVE", live)
    (live / "results_2026.json").write_text(json.dumps(
        {"source": "mock", "completed_matches": []}))
    assert mod.get_results_source() == "mock"


def test_provider_mode_for_helper():
    import importlib
    sys.modules.pop("scripts.live.run_live_update", None)
    mod = importlib.import_module("scripts.live.run_live_update")
    assert mod._provider_mode_for("api_football") == "active"
    assert mod._provider_mode_for("football_data") == "active"
    assert mod._provider_mode_for("mock") == "manual"
    assert mod._provider_mode_for("manual/mock") == "manual"
    assert mod._provider_mode_for("") == "manual"
    assert mod._provider_mode_for(None) == "manual"


# ─── main() integration tests ─────────────────────────────────────────────
# These exercise the orchestrator end-to-end with subprocess calls stubbed,
# closing the mutation-test gap flagged in the Jun-2026 audit:
#   • `and → or` on the 2-gate early-exit at run_live_update.py:431 would
#     skip the sim when ONLY one gate matches → score-correction regression
#   • `!= → ==` on the provider_downgrade comparison would silently emit
#     spurious downgrade warnings on every healthy tick


def _setup_main_test(tmp_path, monkeypatch):
    """Common scaffolding for main() integration tests.

    Returns (mod, live, proc, dash) with all four module-level path
    constants redirected to tmp_path. Each test seeds its own JSON files
    after this returns.
    """
    import importlib
    sys.modules.pop("scripts.live.run_live_update", None)
    mod = importlib.import_module("scripts.live.run_live_update")
    live = tmp_path / "live"; live.mkdir()
    proc = tmp_path / "processed"; proc.mkdir()
    dash = tmp_path / "dashboard"; dash.mkdir()
    monkeypatch.setattr(mod, "LIVE", live)
    monkeypatch.setattr(mod, "PROC", proc)
    monkeypatch.setattr(mod, "DASH", dash)
    monkeypatch.setattr(mod, "CB_PATH", live / "circuit_breaker_state.json")
    monkeypatch.setattr(sys, "argv", ["run_live_update.py"])
    # Clean env so detect_provider_source defaults to manual/mock unless
    # the test overrides — prevents inherited CI env vars from leaking in.
    for var in ("FOOTBALL_PROVIDER", "WC_RESULTS_SOURCE",
                "API_FOOTBALL_KEY", "WC_APIFOOTBALL_KEY",
                "FOOTBALL_DATA_TOKEN", "WC_FOOTBALL_DATA_TOKEN",
                "SPORTMONKS_TOKEN", "WC_SPORTMONKS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return mod, live, proc, dash


def _capture_run(monkeypatch, mod, sim_side_effect=None):
    """Replace mod.run with a recorder that always succeeds. If
    sim_side_effect is given, invoke it when 03_simulate.py is called
    (used to model a successful sim writing its output file)."""
    calls = []
    def fake_run(cmd):
        calls.append(cmd)
        if sim_side_effect and any("03_simulate.py" in part for part in cmd):
            sim_side_effect()
        return 0
    monkeypatch.setattr(mod, "run", fake_run)
    return calls


def test_main_skips_sim_when_count_and_signature_both_match(tmp_path, monkeypatch):
    """The 2-gate early-exit: count_matches AND sig_matches → SKIP sim.
    This is the steady-state no-op path that protects the deploy budget."""
    mod, live, proc, dash = _setup_main_test(tmp_path, monkeypatch)
    locked = {"m": 4, "status": "FT", "home_goals": 2, "away_goals": 1}
    (live / "results_2026.json").write_text(json.dumps({
        "source": "api_football", "completed_matches": [locked], "warnings": [],
    }))
    # Same signature as what get_completed_signature() will compute now —
    # compute it via the helper after seeding so we don't have to hardcode.
    current_sig = mod.get_completed_signature()
    assert current_sig  # sanity: non-empty hash for 1 locked match
    (dash / "predictions_live.json").write_text(json.dumps({
        "completed_matches": [locked], "team_predictions": [],
    }))
    (dash / "live_state.json").write_text(json.dumps({
        "mode": "live", "last_updated_utc": "2026-06-15T12:00:00+00:00",
        "completed_matches_count": 1, "simulation_rerun_this_tick": True,
        "source": "api_football", "provider_mode": "active",
        "warnings": [], "synced_signature": current_sig,
    }))
    calls = _capture_run(monkeypatch, mod)

    rc = mod.main()

    assert rc == 0
    sim_calls = [c for c in calls if any("03_simulate.py" in p for p in c)]
    assert not sim_calls, f"Sim must NOT run on no-op tick; got {sim_calls!r}"
    # The fetch step DOES run (it's how we'd detect score corrections in prod)
    fetch_calls = [c for c in calls if any("fetch_results.py" in p for p in c)]
    assert fetch_calls, "fetch_results.py must always run"


def test_main_forces_resim_when_signature_differs_with_same_count(tmp_path, monkeypatch):
    """SCORE-CORRECTION REGRESSION GUARD: when count matches but the
    content signature differs (e.g. 2-1 corrected to 2-0 on the same
    locked match), main() MUST force the sim subprocess. Mutating
    `prior_sig == current_sig` → `prior_sig != current_sig` or flipping
    the outer `and` would silently skip → this test would fail."""
    mod, live, proc, dash = _setup_main_test(tmp_path, monkeypatch)
    corrected = {"m": 4, "status": "FT", "home_goals": 2, "away_goals": 0}
    (live / "results_2026.json").write_text(json.dumps({
        "source": "api_football", "completed_matches": [corrected], "warnings": [],
    }))
    # predictions_live still has the same COUNT (1 locked) so the count
    # gate alone says "skip". Sim must run because the signature gate fires.
    (dash / "predictions_live.json").write_text(json.dumps({
        "completed_matches": [{"m": 4, "home_goals": 2, "away_goals": 1}],
        "team_predictions": [],
    }))
    (dash / "live_state.json").write_text(json.dumps({
        "mode": "live", "last_updated_utc": "2026-06-15T12:00:00+00:00",
        "completed_matches_count": 1, "simulation_rerun_this_tick": True,
        "source": "api_football", "provider_mode": "active",
        "warnings": [],
        # An old, deliberately-wrong signature that mismatches the corrected
        # results above → the sig-gate fires, forcing a resim.
        "synced_signature": "0000000000000000",
    }))

    def on_sim():
        # Model sim's output: rewrite predictions_live with the corrected score
        (proc / "predictions_live.json").write_text(json.dumps({
            "completed_matches": [corrected], "team_predictions": [],
        }))
    calls = _capture_run(monkeypatch, mod, sim_side_effect=on_sim)

    rc = mod.main()

    assert rc == 0
    sim_calls = [c for c in calls if any("03_simulate.py" in p for p in c)]
    assert sim_calls, "Sim MUST run when signature differs (score correction)"
    # And the new live_state carries the corrected signature
    written = json.loads((dash / "live_state.json").read_text())
    assert written["synced_signature"] != "0000000000000000"
    assert written["synced_signature"] == mod.get_completed_signature()
    assert written["simulation_rerun_this_tick"] is True


def test_main_emits_provider_downgrade_warning_when_env_lies(tmp_path, monkeypatch):
    """When env says api_football but the fetcher actually used mock,
    main() MUST add a `provider_downgrade` warning AND record the truthful
    source. Mutating `actual_source != expected_source` → `==` would
    silently emit a downgrade warning on every healthy tick → this test
    would catch that by checking the negative case in the next test."""
    mod, live, proc, dash = _setup_main_test(tmp_path, monkeypatch)
    monkeypatch.setenv("FOOTBALL_PROVIDER", "api_football")
    monkeypatch.setenv("API_FOOTBALL_KEY", "fake-but-makes-detect-think-its-set")
    # Fetcher reports the truthful source = mock (key was actually invalid or
    # the adapter returned []). This is exactly what fetch_results.py:673,681,690
    # does when it falls through.
    (live / "results_2026.json").write_text(json.dumps({
        "source": "mock", "completed_matches": [], "warnings": [],
    }))
    (dash / "predictions_live.json").write_text(json.dumps({
        "completed_matches": [], "team_predictions": [],
    }))
    (dash / "live_state.json").write_text(json.dumps({
        "mode": "pre_tournament", "last_updated_utc": "2026-06-15T12:00:00+00:00",
        "completed_matches_count": 0, "simulation_rerun_this_tick": False,
        "source": "api_football", "provider_mode": "active",
        "warnings": [],
    }))
    _capture_run(monkeypatch, mod)

    rc = mod.main()

    assert rc == 0
    written = json.loads((dash / "live_state.json").read_text())
    warning_types = {w["type"] for w in written.get("warnings", [])}
    assert "provider_downgrade" in warning_types, (
        f"Expected provider_downgrade warning; got types {warning_types!r}")
    # And the source field reflects reality (mock), not env-detected (api_football)
    assert written["source"] == "mock", (
        f"source must be the truthful 'mock', got {written['source']!r}")
    assert written["provider_mode"] == "manual"


def test_main_no_downgrade_warning_when_env_and_fetcher_agree(tmp_path, monkeypatch):
    """Negative path: when expected source == actual source (healthy tick),
    NO provider_downgrade warning. Inverse of the prior test — together
    they pin both branches of the `!=` comparison."""
    mod, live, proc, dash = _setup_main_test(tmp_path, monkeypatch)
    monkeypatch.setenv("FOOTBALL_PROVIDER", "api_football")
    monkeypatch.setenv("API_FOOTBALL_KEY", "real-key-or-detection-mock")
    (live / "results_2026.json").write_text(json.dumps({
        "source": "api_football", "completed_matches": [], "warnings": [],
    }))
    (dash / "predictions_live.json").write_text(json.dumps({
        "completed_matches": [], "team_predictions": [],
    }))
    (dash / "live_state.json").write_text(json.dumps({
        "mode": "pre_tournament", "last_updated_utc": "2026-06-15T12:00:00+00:00",
        "completed_matches_count": 0, "simulation_rerun_this_tick": False,
        "source": "api_football", "provider_mode": "active",
        "warnings": [],
    }))
    _capture_run(monkeypatch, mod)

    rc = mod.main()

    assert rc == 0
    written = json.loads((dash / "live_state.json").read_text())
    warning_types = {w["type"] for w in written.get("warnings", [])}
    assert "provider_downgrade" not in warning_types, (
        f"No downgrade warning expected when env == actual; got {warning_types!r}")
    assert written["source"] == "api_football"


def test_main_writes_fetch_failure_warning_and_does_not_trip_cb(tmp_path, monkeypatch):
    """fetch_results.py exit-nonzero is treated as transient: write a
    `fetch_failure` warning, RETURN 0 (so the Vercel cron / GHA workflow
    doesn't escalate), and do NOT increment the circuit-breaker
    (run_live_update.py:391-398). This was uncovered before the Jun-2026
    rev — adding it here so a refactor that mistakenly trips the CB on
    transient API blips would fail the suite."""
    mod, live, proc, dash = _setup_main_test(tmp_path, monkeypatch)
    (live / "results_2026.json").write_text(json.dumps({
        "source": "api_football", "completed_matches": [], "warnings": [],
    }))
    (dash / "predictions_live.json").write_text(json.dumps({
        "completed_matches": [], "team_predictions": [],
    }))
    (dash / "live_state.json").write_text(json.dumps({
        "mode": "pre_tournament", "last_updated_utc": "2026-06-15T12:00:00+00:00",
        "completed_matches_count": 0, "simulation_rerun_this_tick": False,
        "source": "api_football", "provider_mode": "active",
        "warnings": [],
    }))

    def fake_run(cmd):
        if any("fetch_results.py" in p for p in cmd):
            return 1  # fetcher fails this tick
        return 0
    monkeypatch.setattr(mod, "run", fake_run)

    rc = mod.main()

    assert rc == 0  # transient; do not escalate to CI red
    written = json.loads((dash / "live_state.json").read_text())
    types = {w["type"] for w in written.get("warnings", [])}
    assert "fetch_failure" in types
    # CB must not have been written (or, if written, must be zero) — fetch
    # failures are not a sim-failure class.
    cb_path = live / "circuit_breaker_state.json"
    if cb_path.exists():
        cb = json.loads(cb_path.read_text())
        assert cb.get("consecutive_failures", 0) == 0, (
            f"fetch_failure must not increment CB; got {cb}")


def test_main_increments_cb_and_preserves_predictions_on_sim_failure(tmp_path, monkeypatch):
    """When 03_simulate.py exits non-zero (line 462), main() must:
      • increment circuit_breaker_state.json (line 464)
      • emit a sim_failure warning to live_state (lines 465-472)
      • return 1
      • NOT overwrite the prior predictions_live.json
        (the preservation contract — sim-fail must keep stale-but-valid
        data rather than truncate to a half-written file).
    """
    mod, live, proc, dash = _setup_main_test(tmp_path, monkeypatch)
    corrected = {"m": 4, "status": "FT", "home_goals": 2, "away_goals": 0}
    (live / "results_2026.json").write_text(json.dumps({
        "source": "api_football", "completed_matches": [corrected], "warnings": [],
    }))
    original_live_predictions = {
        "completed_matches": [{"m": 4, "home_goals": 2, "away_goals": 1}],
        "team_predictions": [{"team": "Spain", "p_champion": 0.24}],
    }
    # PROC seed = the "prior valid sim output" the preservation contract guards.
    # DASH seed = mirrors what was last synced to production so the count gate
    # passes (count_matches=True) and the SIGNATURE gate is what forces resim —
    # which is the exact regression path this test is designed to exercise.
    (proc / "predictions_live.json").write_text(json.dumps(original_live_predictions))
    (dash / "predictions_live.json").write_text(json.dumps(original_live_predictions))
    (dash / "live_state.json").write_text(json.dumps({
        "mode": "live", "last_updated_utc": "2026-06-15T12:00:00+00:00",
        "completed_matches_count": 1, "simulation_rerun_this_tick": True,
        "source": "api_football", "provider_mode": "active",
        "warnings": [],
        # Wrong signature → main() forces resim → sim fails → assertions fire
        "synced_signature": "stale_sig_does_not_match",
    }))

    def fake_run(cmd):
        if any("03_simulate.py" in p for p in cmd):
            return 1  # sim fails
        return 0
    monkeypatch.setattr(mod, "run", fake_run)

    rc = mod.main()

    assert rc == 1
    written = json.loads((dash / "live_state.json").read_text())
    types = {w["type"] for w in written.get("warnings", [])}
    assert "sim_failure" in types
    # CB incremented from 0 → 1
    cb = json.loads((live / "circuit_breaker_state.json").read_text())
    assert cb["consecutive_failures"] == 1
    # The preservation contract: predictions_live.json must be untouched.
    # If a refactor accidentally writes a partial file here, this assert
    # catches the regression.
    preserved = json.loads((proc / "predictions_live.json").read_text())
    assert preserved == original_live_predictions


def test_sigterm_handler_writes_tick_interrupted_and_exits_143(tmp_path, monkeypatch):
    """Closes mutation M7 (handler body) from the Jun-2026 audit: removing
    the body would silently kill the orchestrator mid-tick with no
    dashboard signal. This test invokes the installed handler directly
    (signal-delivery in pytest is fragile; we can't reliably send the
    signal but we CAN call the captured handler function) and pins:
      - sys.exit(143) propagates (the SIGTERM convention: 128 + 15)
      - a `tick_interrupted` warning lands in live_state.json before exit

    Skip-vs-fail policy: distinguish "off main thread" (legitimate skip —
    signal.signal raises ValueError there) from "main thread but install
    silently no-op'd" (a real regression — fail). The previous skip-on-any
    failure pattern would have masked mutation M7b (gut the handler body to
    `pass`); the discriminating probe below catches it.
    """
    import signal
    mod, live, proc, dash = _setup_main_test(tmp_path, monkeypatch)
    (live / "results_2026.json").write_text(json.dumps({
        "source": "api_football", "completed_matches": [], "warnings": [],
    }))
    (dash / "live_state.json").write_text(json.dumps({
        "mode": "live", "last_updated_utc": "2026-06-15T12:00:00+00:00",
        "completed_matches_count": 0, "simulation_rerun_this_tick": True,
        "source": "api_football", "provider_mode": "active",
        "warnings": [],
    }))

    orig = signal.getsignal(signal.SIGTERM)
    try:
        # Discriminating probe: can ANY signal handler install on this
        # thread? If signal.signal raises, we're off main thread → skip.
        # If it succeeds, the production helper MUST also succeed.
        try:
            signal.signal(signal.SIGTERM, orig)
            on_main_thread = True
        except (ValueError, OSError):
            on_main_thread = False
        if not on_main_thread:
            pytest.skip("not on main thread; signal.signal cannot install — "
                        "test environment limitation, not a production bug")

        mod._install_sigterm_handler()
        handler = signal.getsignal(signal.SIGTERM)
        # We're on main thread, so install MUST have worked. A non-callable
        # or SIG_DFL/SIG_IGN result here = mutation M7b survived → fail.
        if not callable(handler) or handler in (signal.SIG_DFL, signal.SIG_IGN):
            pytest.fail(
                f"_install_sigterm_handler() ran on main thread but no callable "
                f"handler was registered (got {handler!r}); production regression")

        with pytest.raises(SystemExit) as exc:
            handler(signal.SIGTERM, None)
        assert exc.value.code == 143

        written = json.loads((dash / "live_state.json").read_text())
        types = {w["type"] for w in written.get("warnings", [])}
        assert "tick_interrupted" in types, (
            f"SIGTERM must write a tick_interrupted warning; got {types!r}")
    finally:
        signal.signal(signal.SIGTERM, orig)


def test_main_invokes_sigterm_handler_install(tmp_path, monkeypatch):
    """Closes mutation M7a from the Jun-2026 audit: pin that main() actually
    *calls* _install_sigterm_handler() per tick. The behavioural test above
    invokes the handler directly and would still pass if a future commit
    silently removed the install call from main() — the handler would
    simply never be registered in production. This recorder-based test
    catches that exact regression."""
    mod, live, proc, dash = _setup_main_test(tmp_path, monkeypatch)
    # Minimal state for early-exit path so main() returns cleanly.
    (live / "results_2026.json").write_text(json.dumps({
        "source": "api_football", "completed_matches": [], "warnings": [],
    }))
    (dash / "predictions_live.json").write_text(json.dumps({
        "completed_matches": [], "team_predictions": [],
    }))
    (dash / "live_state.json").write_text(json.dumps({
        "mode": "pre_tournament", "last_updated_utc": "2026-06-15T12:00:00+00:00",
        "completed_matches_count": 0, "simulation_rerun_this_tick": False,
        "source": "api_football", "provider_mode": "active",
        "warnings": [],
    }))

    install_calls = []
    def recorder():
        install_calls.append(1)
    monkeypatch.setattr(mod, "_install_sigterm_handler", recorder)
    _capture_run(monkeypatch, mod)

    rc = mod.main()

    assert rc == 0
    assert len(install_calls) == 1, (
        f"main() must invoke _install_sigterm_handler() exactly once per tick; "
        f"got {len(install_calls)} call(s)")


def test_main_falls_back_to_expected_source_when_results_source_missing(tmp_path, monkeypatch):
    """Closes mutation M11: pin the `or expected_source` fallback at
    run_live_update.py:411. If results_2026.json is missing the `source`
    field (corrupted partial write or schema regression),
    `get_results_source()` returns "" and the fallback must kick in —
    otherwise live_state.json gets `source=""`, which downstream
    consumers (providerLabel in app.js) render as "manual / mock"
    instead of the truthful env-detected value."""
    mod, live, proc, dash = _setup_main_test(tmp_path, monkeypatch)
    monkeypatch.setenv("FOOTBALL_PROVIDER", "api_football")
    monkeypatch.setenv("API_FOOTBALL_KEY", "fake-key")
    # results_2026.json deliberately omits the `source` key
    (live / "results_2026.json").write_text(json.dumps({
        "completed_matches": [], "warnings": [],
    }))
    (dash / "predictions_live.json").write_text(json.dumps({
        "completed_matches": [], "team_predictions": [],
    }))
    (dash / "live_state.json").write_text(json.dumps({
        "mode": "pre_tournament", "last_updated_utc": "2026-06-15T12:00:00+00:00",
        "completed_matches_count": 0, "simulation_rerun_this_tick": False,
        "source": "api_football", "provider_mode": "active",
        "warnings": [],
    }))
    _capture_run(monkeypatch, mod)

    rc = mod.main()

    assert rc == 0
    written = json.loads((dash / "live_state.json").read_text())
    # Source must NOT be empty — must fall back to env-detected.
    assert written["source"] == "api_football", (
        f"missing-source-field must fall back to expected; got {written['source']!r}")
    assert written["provider_mode"] == "active"


def test_main_emits_provider_downgrade_warning_on_sim_path_too(tmp_path, monkeypatch):
    """Defence against a refactor that moves the downgrade-emit logic
    AFTER the early-exit gate. T3/T4 pin the early-exit path; this test
    pins the sim path. Both must work because the contract is "downgrade
    warning is always surfaced when expected ≠ actual"."""
    mod, live, proc, dash = _setup_main_test(tmp_path, monkeypatch)
    monkeypatch.setenv("FOOTBALL_PROVIDER", "api_football")
    monkeypatch.setenv("API_FOOTBALL_KEY", "fake-key-detection-only")
    corrected = {"m": 4, "status": "FT", "home_goals": 2, "away_goals": 0}
    # source=mock from fetcher + env says api_football → downgrade
    (live / "results_2026.json").write_text(json.dumps({
        "source": "mock", "completed_matches": [corrected], "warnings": [],
    }))
    (dash / "predictions_live.json").write_text(json.dumps({
        "completed_matches": [{"m": 4, "home_goals": 2, "away_goals": 1}],
        "team_predictions": [],
    }))
    # synced_signature mismatch → forces sim path (not early-exit)
    (dash / "live_state.json").write_text(json.dumps({
        "mode": "live", "last_updated_utc": "2026-06-15T12:00:00+00:00",
        "completed_matches_count": 1, "simulation_rerun_this_tick": True,
        "source": "api_football", "provider_mode": "active",
        "warnings": [], "synced_signature": "stale_sig",
    }))

    def on_sim():
        (proc / "predictions_live.json").write_text(json.dumps({
            "completed_matches": [corrected], "team_predictions": [],
        }))
    calls = _capture_run(monkeypatch, mod, sim_side_effect=on_sim)

    rc = mod.main()

    assert rc == 0
    sim_calls = [c for c in calls if any("03_simulate.py" in p for p in c)]
    assert sim_calls, "Setup precondition: sim must run on this branch"
    written = json.loads((dash / "live_state.json").read_text())
    types = {w["type"] for w in written.get("warnings", [])}
    assert "provider_downgrade" in types, (
        f"Downgrade warning must survive the sim path; got {types!r}")
    assert written["source"] == "mock"  # truthful, not env-detected
    assert written["simulation_rerun_this_tick"] is True
