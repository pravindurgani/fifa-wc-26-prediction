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
import json
import os
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
    except Exception:
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
    except Exception:
        return 0


def get_results_warnings() -> list:
    p = LIVE / "results_2026.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text()).get("warnings", []) or []
    except Exception:
        return []


def get_live_predictions_locked_count() -> int:
    """How many matches were locked in the most recent predictions_live.json."""
    p = PROC / "predictions_live.json"
    if not p.exists():
        return -1
    try:
        return len(json.loads(p.read_text()).get("completed_matches", []))
    except Exception:
        return -1


def write_live_state(mode: str, completed_count: int, sim_rerun: bool,
                     warnings: list | None = None, source: str = "manual/mock"):
    """Atomic live_state.json write."""
    state = {
        "mode": mode,
        "last_updated_utc": datetime.now(timezone.utc).isoformat(),
        "completed_matches_count": completed_count,
        "simulation_rerun_this_tick": sim_rerun,
        "source": source,
        "warnings": warnings or [],
    }
    atomic_write_json(DASH / "live_state.json", state)
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
        print(f"[run_live_update] could not parse predictions for delta: {e}")
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


def main() -> int:
    print("== Live update tick ==")

    failures = read_circuit_breaker()
    if failures >= CB_THRESHOLD:
        msg = f"Circuit breaker tripped after {failures} consecutive failures. " \
              f"Manual intervention required: reset by deleting {CB_PATH}."
        print(f"[run_live_update] {msg}")
        # Still emit live_state so the dashboard reflects the situation
        write_live_state("live", get_completed_count(), sim_rerun=False,
                         warnings=[{"type": "circuit_breaker", "message": msg}])
        return 2

    # Step 1: fetch results
    rc = run([sys.executable, "scripts/live/fetch_results.py"])
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

    # Step 2: early exit if predictions_live already reflects current locked count
    if last_synced == new_count and last_synced >= 0:
        print(f"[run_live_update] predictions_live.json already at {new_count} locked matches — skipping sim")
        mode = "pre_tournament" if new_count == 0 else "live"
        write_live_state(mode, new_count, sim_rerun=False, warnings=warns)
        write_circuit_breaker(0)  # success path resets
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
                         }])
        return 1

    # Success: reset breaker
    write_circuit_breaker(0)

    # Step 5: live delta — only meaningful once matches are locked
    if new_count > 0:
        delta = build_live_delta()
    else:
        delta = write_empty_delta()

    # Step 6: live state
    mode = "live" if new_count > 0 else "pre_tournament"
    write_live_state(mode, new_count, sim_rerun=True, warnings=warns)

    # Step 7: copy to dashboard (atomic via rename)
    src = PROC / "predictions_live.json"
    if src.exists():
        dst = DASH / "predictions_live.json"
        try:
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            tmp.write_bytes(src.read_bytes())
            os.replace(tmp, dst)
        except Exception as e:
            print(f"[run_live_update] failed to copy predictions_live.json to dashboard: {e}")

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
