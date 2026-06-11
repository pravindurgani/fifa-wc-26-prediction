"""
run_live_update.py — Live update orchestrator.

Idempotent. Safe to run every 10-15 minutes during the tournament.

Flow:
  1. Fetch results (fetch_results.py) — populates results_2026.json
  2. Diff vs previous run — exit early if no new FT matches
  3. Re-compute soft team-state Elo deltas (update_team_state.py)
  4. Re-run live simulation (03_simulate.py --live)
  5. Build live_delta.json (predictions_static vs predictions_live)
  6. Write live_state.json with mode, last_updated, etc.
  7. Copy artifacts to dashboard/
  8. Run validator

Hardening (Jun 2026):
  - Atomic writes for live_state.json and live_delta.json
  - Sim failure preserves the previous predictions_live.json (no corrupt overwrite)
  - Postponed/abandoned matches surface as warnings on live_state
  - Circuit breaker: 3 consecutive sim failures backs off and writes
    {"mode": "live", "warning": "..."} until a human intervenes
  - Top-level try/except so a partial crash still produces a usable live_state
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "live"
PROC = ROOT / "data" / "processed"
DASH = ROOT / "dashboard"
CB_PATH = LIVE / "circuit_breaker_state.json"
CB_THRESHOLD = 3  # consecutive sim failures before tripping the breaker


def run(cmd: list[str]) -> int:
    print(f"  → {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def atomic_write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def read_circuit_breaker() -> int:
    if not CB_PATH.exists():
        return 0
    try:
        return int(json.loads(CB_PATH.read_text()).get("consecutive_failures", 0))
    except Exception as e:
        print(f"[run_live_update] could not read {CB_PATH.name}: {type(e).__name__}: {e}")
        return 0


def write_circuit_breaker(failures: int):
    atomic_write_json(CB_PATH, {
        "consecutive_failures": failures,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "threshold": CB_THRESHOLD,
    })


def get_completed_count() -> int:
    p = LIVE / "results_2026.json"
    if not p.exists():
        return 0
    try:
        return len(json.loads(p.read_text()).get("completed_matches", []))
    except Exception as e:
        print(f"[run_live_update] could not parse {p.name} for count: {type(e).__name__}: {e}")
        return 0


def get_results_warnings() -> list:
    p = LIVE / "results_2026.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text()).get("warnings", []) or []
    except Exception as e:
        print(f"[run_live_update] could not parse {p.name} for warnings: {type(e).__name__}: {e}")
        return []


def get_live_predictions_locked_count() -> int:
    """How many matches were locked in the most recent predictions_live.json."""
    p = PROC / "predictions_live.json"
    if not p.exists():
        return -1
    try:
        return len(json.loads(p.read_text()).get("completed_matches", []))
    except Exception as e:
        print(f"[run_live_update] could not parse {p.name} for locked count: {type(e).__name__}: {e}")
        return -1


def get_completed_signature() -> str:
    """Stable content hash of `completed_matches` in results_2026.json.

    Used as a defence-in-depth check on top of `get_completed_count()`:
    if the API corrects a score on an already-locked match (e.g. 2-1 → 2-0)
    the completed count is unchanged, so the count-only early-exit at
    main() Step 2 would skip the sim and leave predictions_live.json
    pinned to the wrong scoreline. Comparing this signature to the prior
    `synced_signature` recorded in live_state.json catches that case.

    Excludes volatile per-record `updated_at` (mirroring fetch_results.py's
    `_material_results._scrub`) so genuinely-no-op ticks still hash to the
    same value.
    """
    p = LIVE / "results_2026.json"
    if not p.exists():
        return ""
    try:
        matches = json.loads(p.read_text()).get("completed_matches", []) or []
        canon = sorted(
            ({k: v for k, v in m.items() if k != "updated_at"} for m in matches),
            key=lambda m: str(m.get("m", "")),
        )
        blob = json.dumps(canon, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    except Exception as e:
        # Falling back to "" disables signature-based resim trigger for this
        # tick — count-only check still applies, so the worst case is the
        # pre-Jun-2026 behaviour (no score-correction detection). Surface the
        # cause so a corrupted results_2026.json is debuggable.
        print(f"[run_live_update] could not compute signature from {p.name}: "
              f"{type(e).__name__}: {e}")
        return ""


def get_results_source() -> str:
    """Return the `source` field that fetch_results.py wrote to
    results_2026.json after running its adapter. This is the *truthful*
    source — e.g. "mock" when API_FOOTBALL_KEY was missing and the
    fetcher silently fell back. Returns "" when the file is absent."""
    p = LIVE / "results_2026.json"
    if not p.exists():
        return ""
    try:
        return json.loads(p.read_text()).get("source", "") or ""
    except Exception as e:
        print(f"[run_live_update] could not parse {p.name} for source: "
              f"{type(e).__name__}: {e}")
        return ""


def _provider_mode_for(source: str) -> str:
    """Provider_mode is 'active' iff source identifies a real upstream.
    mock / manual / empty → 'manual'."""
    s = (source or "").strip().lower()
    if s in ("", "mock", "manual", "manual/mock"):
        return "manual"
    return "active"


def detect_provider_source() -> tuple[str, str]:
    """Returns (source_label, provider_mode).

    source_label: human-readable string surfaced on the dashboard
    provider_mode: 'active' if a real provider key is configured, else 'manual'
    """
    provider = (os.environ.get("FOOTBALL_PROVIDER")
                or os.environ.get("WC_RESULTS_SOURCE")
                or "mock").strip().lower().replace("-", "_")
    apifootball_key = (os.environ.get("API_FOOTBALL_KEY")
                       or os.environ.get("WC_APIFOOTBALL_KEY"))
    football_data_token = (os.environ.get("FOOTBALL_DATA_TOKEN")
                           or os.environ.get("WC_FOOTBALL_DATA_TOKEN"))
    sportmonks_token = (os.environ.get("SPORTMONKS_TOKEN")
                        or os.environ.get("WC_SPORTMONKS_TOKEN"))
    if provider in ("api_football", "apifootball") and apifootball_key:
        return "api_football", "active"
    if provider in ("football_data", "footballdata") and football_data_token:
        return "football_data", "active"
    if provider == "sportmonks" and sportmonks_token:
        return "sportmonks", "active"
    return "manual/mock", "manual"


def write_live_state(mode: str, completed_count: int, sim_rerun: bool,
                     warnings: list | None = None, source: str | None = None,
                     provider_mode: str | None = None,
                     synced_signature: str | None = None):
    """Atomic live_state.json write.

    Preserves the previous `last_updated_utc` when no material field changed
    so unchanged ticks produce a byte-identical file (no git diff → no
    commit → no deploy). Material fields: mode, completed_matches_count,
    simulation_rerun_this_tick, source, provider_mode, warnings,
    synced_signature.

    `synced_signature` is the hash of the results_2026.json content that
    the most recent successful sim was run against. It is "sticky" — when
    not provided, it is preserved from the prior file so that no-op ticks
    don't overwrite the last-good marker. Pass an explicit value only when
    a sim has just succeeded.
    """
    prev = None
    state_path = DASH / "live_state.json"
    try:
        if state_path.exists():
            prev = json.loads(state_path.read_text())
    except Exception as e:
        # Corrupted prior file → treat as no prior state, bump fresh. Logging
        # this is essential: a silent reset to env-var-detected source can
        # mask a real disk-write race or filesystem fault.
        print(f"[run_live_update] prior live_state.json unreadable, will bump fresh: "
              f"{type(e).__name__}: {e}")
        prev = None

    # Source / provider_mode resolution: explicit arg → prior file → env detect.
    # Preserving from the prior file matters for paths like the circuit-breaker
    # or fatal-crash handler where we haven't actually run a fetch and so don't
    # know the truthful source — we'd rather keep the last known value than
    # silently revert to env-var detection (which lies on downgrade).
    if source is None and prev is not None:
        source = prev.get("source")
    if provider_mode is None and prev is not None:
        provider_mode = prev.get("provider_mode")
    if source is None or provider_mode is None:
        auto_source, auto_mode = detect_provider_source()
        source = source or auto_source
        provider_mode = provider_mode or auto_mode

    if synced_signature is None:
        synced_signature = (prev or {}).get("synced_signature", "")

    warnings = warnings or []

    def _material(s):
        return (s.get("mode"),
                s.get("completed_matches_count"),
                bool(s.get("simulation_rerun_this_tick")),
                s.get("source"),
                s.get("provider_mode"),
                json.dumps(s.get("warnings") or [], sort_keys=True),
                s.get("synced_signature") or "")

    new_state_no_ts = {
        "mode": mode,
        "completed_matches_count": completed_count,
        "simulation_rerun_this_tick": sim_rerun,
        "source": source,
        "provider_mode": provider_mode,
        "warnings": warnings,
        "synced_signature": synced_signature,
    }
    if prev and _material(prev) == _material(new_state_no_ts):
        # Material state unchanged — preserve prior timestamp so the on-disk
        # file is byte-identical to the previous tick.
        ts = prev.get("last_updated_utc") or datetime.now(timezone.utc).isoformat()
    else:
        ts = datetime.now(timezone.utc).isoformat()

    state = {
        "mode": mode,
        "last_updated_utc": ts,
        "completed_matches_count": completed_count,
        "simulation_rerun_this_tick": sim_rerun,
        "source": source,
        "provider_mode": provider_mode,
        "warnings": warnings,
        "synced_signature": synced_signature,
    }
    atomic_write_json(state_path, state)
    return state


def write_empty_delta():
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "top_movers_up": [], "top_movers_down": [], "all_movers": [],
    }
    atomic_write_json(DASH / "live_delta.json", out)
    return out


def build_live_delta(min_pp: float = 0.5):
    """Diff predictions_static.json vs predictions_live.json → live_delta.json.

    `min_pp` filters out movers below the seed-noise threshold (~0.3-0.5pp
    for 5×5000 vs 3×3000 sample sizes). Pre-tournament deltas should be
    written via write_empty_delta() instead.
    """
    static_p = PROC / "predictions.json"
    live_p = PROC / "predictions_live.json"
    if not static_p.exists() or not live_p.exists():
        return None
    try:
        s = json.loads(static_p.read_text())
        l = json.loads(live_p.read_text())
    except Exception as e:
        print(f"[run_live_update] could not parse predictions for delta: "
              f"{type(e).__name__}: {e}")
        return None
    static_p_by_t = {t["team"]: t["p_champion"] for t in s.get("team_predictions", [])}
    live_p_by_t = {t["team"]: t["p_champion"] for t in l.get("team_predictions", [])}
    movers = []
    for team, lp in live_p_by_t.items():
        sp = static_p_by_t.get(team, 0)
        delta_pp = (lp - sp) * 100
        if abs(delta_pp) < min_pp:
            continue
        movers.append({"team": team, "static": sp, "live": lp, "delta_pp": delta_pp})
    movers.sort(key=lambda d: -abs(d["delta_pp"]))
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "min_pp_threshold": min_pp,
        "top_movers_up":   [m for m in movers if m["delta_pp"] > 0][:10],
        "top_movers_down": [m for m in movers if m["delta_pp"] < 0][:10],
        "all_movers": movers,
    }
    atomic_write_json(DASH / "live_delta.json", out)
    return out


def _install_sigterm_handler():
    """Write a clean `tick_interrupted` warning to live_state.json on SIGTERM
    before exiting. GitHub Actions sends SIGTERM when `timeout-minutes:` is
    hit (live-matchday.yml:47, 25-min cap) and again on manual cancellation.
    Without this, the process is killed mid-sim and the dashboard shows no
    sign anything went wrong — users can't tell whether the silence is a
    no-op tick or a dropped tick. The handler MUST NOT raise: best-effort
    write wrapped in bare try/except, then exit 143 (128 + SIGTERM)."""
    def _term(signum, _frame):
        try:
            write_live_state(
                "live" if get_completed_count() > 0 else "pre_tournament",
                get_completed_count(),
                sim_rerun=False,
                warnings=[{"type": "tick_interrupted",
                           "message": f"orchestrator received signal {signum}; "
                                      "tick aborted mid-flight"}],
            )
        except Exception:
            pass
        sys.exit(143)
    try:
        signal.signal(signal.SIGTERM, _term)
    except (ValueError, OSError):
        # Not in main thread (pytest, embedded runs) — silently skip.
        pass


def _get_prior_synced_signature() -> str:
    """Read the last-good `synced_signature` from dashboard/live_state.json.
    Returns "" when no prior file or no field — callers treat that as
    "no prior sig recorded, fall back to count-only check"."""
    p = DASH / "live_state.json"
    if not p.exists():
        return ""
    try:
        return json.loads(p.read_text()).get("synced_signature", "") or ""
    except Exception as e:
        print(f"[run_live_update] could not read prior synced_signature: "
              f"{type(e).__name__}: {e}")
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None,
                    help="Override provider (mock | api_football | sportmonks). "
                         "Default: FOOTBALL_PROVIDER env, then WC_RESULTS_SOURCE, then mock.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch + diff but do not re-simulate or write dashboard JSON.")
    args = ap.parse_args()

    _install_sigterm_handler()

    if args.provider:
        os.environ["FOOTBALL_PROVIDER"] = args.provider

    print("== Live update tick ==" + (" [dry-run]" if args.dry_run else ""))

    failures = read_circuit_breaker()
    if failures >= CB_THRESHOLD:
        msg = f"Circuit breaker tripped after {failures} consecutive failures. " \
              f"Manual intervention required: reset by deleting {CB_PATH}."
        print(f"[run_live_update] {msg}")
        # Still emit live_state so the dashboard reflects the situation.
        # source/provider_mode are intentionally omitted → write_live_state
        # preserves the prior file's values rather than reverting to env-var
        # detection (which can lie under a downgrade).
        write_live_state("live", get_completed_count(), sim_rerun=False,
                         warnings=[{"type": "circuit_breaker", "message": msg}])
        return 2

    # Step 1: fetch results (pass --dry-run through)
    fetch_cmd = [sys.executable, "scripts/live/fetch_results.py"]
    if args.dry_run:
        fetch_cmd.append("--dry-run")
    rc = run(fetch_cmd)
    if rc != 0:
        print("[run_live_update] fetch_results failed — emitting warning, keeping prior state")
        write_live_state("live" if get_completed_count() > 0 else "pre_tournament",
                         get_completed_count(), sim_rerun=False,
                         warnings=[{"type": "fetch_failure",
                                    "message": "Live result fetcher exited non-zero; "
                                               "previous predictions retained."}])
        return 0  # don't trip CB for fetch failure — that's transient

    new_count = get_completed_count()
    warns = get_results_warnings()
    last_synced = get_live_predictions_locked_count()
    current_sig = get_completed_signature()
    prior_sig = _get_prior_synced_signature()

    # Surface a `provider_downgrade` warning when the env vars said one thing
    # but fetch_results.py actually used another (e.g. API_FOOTBALL_KEY missing
    # → silent fall-through to mock). The dashboard previously read `source`
    # from env-var detection only and lied about its data source on downgrade.
    expected_source, _expected_mode = detect_provider_source()
    actual_source = get_results_source() or expected_source
    actual_mode = _provider_mode_for(actual_source)
    if (actual_source and expected_source
            and expected_source != "manual/mock"
            and actual_source != expected_source):
        warns = warns + [{
            "type": "provider_downgrade",
            "message": (f"expected source {expected_source!r} but fetcher reported "
                        f"{actual_source!r}; check provider credentials."),
        }]

    # Step 2: early exit if predictions_live already reflects current locked
    # content. Two gates:
    #   - Count matches predictions_live's locked count (cheap, sufficient
    #     when no priors exist or first run).
    #   - When a prior synced_signature is on file, it must also match the
    #     current content hash. This catches score corrections on already-
    #     locked matches: same count, different scoreline → must resim.
    count_matches = last_synced == new_count and last_synced >= 0
    sig_matches_or_unknown = (not prior_sig) or (prior_sig == current_sig)
    if count_matches and sig_matches_or_unknown:
        print(f"[run_live_update] predictions_live.json already at {new_count} locked matches "
              f"(content sig {current_sig or 'n/a'}) — skipping sim")
        mode = "pre_tournament" if new_count == 0 else "live"
        write_live_state(mode, new_count, sim_rerun=False, warnings=warns,
                         source=actual_source, provider_mode=actual_mode)
        write_circuit_breaker(0)  # success path resets
        return 0

    if count_matches and not sig_matches_or_unknown:
        print(f"[run_live_update] count unchanged ({new_count}) but content sig "
              f"differs (prior={prior_sig} new={current_sig}) — forcing resim")

    if args.dry_run:
        print(f"[run_live_update] dry-run: would re-simulate "
              f"({last_synced} locked → {new_count} locked, sig {prior_sig}→{current_sig})")
        mode = "pre_tournament" if new_count == 0 else "live"
        write_live_state(mode, new_count, sim_rerun=False, warnings=warns,
                         source=actual_source, provider_mode=actual_mode)
        return 0

    # Step 3: update team state (soft Elo deltas) — non-fatal if it fails
    rc = run([sys.executable, "scripts/live/update_team_state.py"])
    if rc != 0:
        print("[run_live_update] update_team_state failed; continuing without it")

    # Step 4: re-run live simulation
    print(f"[run_live_update] {new_count} matches completed, re-simulating…")
    rc = run([sys.executable, "scripts/03_simulate.py",
              "--live", "--seeds", "3", "--sims", "3000",
              "--out", "predictions_live.json"])
    if rc != 0:
        new_failures = failures + 1
        write_circuit_breaker(new_failures)
        write_live_state("live" if new_count > 0 else "pre_tournament",
                         new_count, sim_rerun=False,
                         warnings=warns + [{
                             "type": "sim_failure",
                             "message": f"Live simulation failed ({new_failures}/{CB_THRESHOLD}); "
                                        "previous predictions_live.json retained.",
                         }],
                         source=actual_source, provider_mode=actual_mode)
        return 1

    # Success: reset breaker
    write_circuit_breaker(0)

    # Step 5: live delta — only meaningful once matches are locked
    if new_count > 0:
        delta = build_live_delta()
    else:
        delta = write_empty_delta()

    # Step 6: live state — pass synced_signature so the next tick can detect
    # score corrections via signature comparison (Step 2 above).
    mode = "live" if new_count > 0 else "pre_tournament"
    write_live_state(mode, new_count, sim_rerun=True, warnings=warns,
                     source=actual_source, provider_mode=actual_mode,
                     synced_signature=current_sig)

    # Step 7: copy to dashboard (atomic via rename)
    src = PROC / "predictions_live.json"
    if src.exists():
        dst = DASH / "predictions_live.json"
        try:
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            tmp.write_bytes(src.read_bytes())
            os.replace(tmp, dst)
        except Exception as e:
            print(f"[run_live_update] failed to copy predictions_live.json to dashboard: "
                  f"{type(e).__name__}: {e}")

    # Step 8: validator
    run([sys.executable, "scripts/09_validate.py"])

    print(f"[run_live_update] DONE — locked {new_count} matches")
    if delta and delta.get("top_movers_up"):
        top = delta["top_movers_up"][0]
        print(f"  Top mover: {top['team']} ({top['delta_pp']:+.2f}pp)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[run_live_update] FATAL {type(e).__name__}: {e}")
        traceback.print_exc()
        # Best-effort: write a warning to live_state so the dashboard knows
        try:
            write_live_state("live" if get_completed_count() > 0 else "pre_tournament",
                             get_completed_count(), sim_rerun=False,
                             warnings=[{"type": "orchestrator_crash",
                                        "message": f"{type(e).__name__}: {e}"[:200]}])
        except Exception:
            pass
        sys.exit(1)
