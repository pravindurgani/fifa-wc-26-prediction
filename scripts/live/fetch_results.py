"""
fetch_results.py — Pluggable live-score fetcher.

Sources (selected via env var WC_RESULTS_SOURCE):
  - mock (default) — reads data/live/results_2026.json as-is (manual mode)
  - apifootball — placeholder adapter for API-Football (requires WC_APIFOOTBALL_KEY)
  - sportmonks  — placeholder adapter for Sportmonks (requires WC_SPORTMONKS_TOKEN)

Each adapter must return a list of {m, date, home, away, home_score, away_score,
status, source, updated_at} where status ∈ {SCHEDULED, LIVE, FT, AET, PEN,
POSTPONED, ABANDONED, CANCELED, SUSPENDED}. We only LOCK status in (FT, AET,
PEN). POSTPONED/ABANDONED/CANCELED are tracked and emitted as warnings so the
orchestrator can surface them to the dashboard.

Hardening (Jun 2026):
  - All adapter calls wrapped in try/except — provider 5xx never crashes CI.
  - Match-level validation wrapped per-record — one bad row never poisons the
    whole batch.
  - results_2026.json is written atomically (write .tmp, then rename).
  - If validation produces zero valid records *and* an existing file exists,
    we keep the existing file (refuse to overwrite with empty).
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "live"
RAW = ROOT / "data" / "raw"

LOCKED_STATUSES = {"FT", "AET", "PEN"}
WARN_STATUSES = {"POSTPONED", "ABANDONED", "CANCELED", "CANCELLED", "SUSPENDED"}


def fetch_mock():
    """Mock: return whatever's already in results_2026.json (manual entry mode)."""
    path = LIVE / "results_2026.json"
    if not path.exists():
        return []
    try:
        d = json.loads(path.read_text())
    except Exception as e:
        print(f"[fetch_results] mock read failed: {e}")
        return []
    return d.get("completed_matches", [])


def fetch_apifootball(api_key: str):
    """Placeholder for API-Football adapter. Real implementation would:
      GET https://v3.football.api-sports.io/fixtures?league=1&season=2026
      Headers: x-apisports-key
      Map fixture.status.short ∈ {FT, AET, PEN, NS, 1H, 2H, ...} → our schema.
      Filter to FT/AET/PEN, map to our match numbers via wc2026_config.json.

    Until wired:
      - logs the missing implementation,
      - returns an empty list (graceful no-op),
      - does NOT raise.
    """
    print("[fetch_results] apifootball adapter not yet wired — returning empty list "
          "(orchestrator will keep the existing predictions_live.json untouched).")
    return []


def fetch_sportmonks(token: str):
    """Placeholder for Sportmonks adapter — see fetch_apifootball docstring."""
    print("[fetch_results] sportmonks adapter not yet wired — returning empty list.")
    return []


def validate_match(m: dict, schedule: list) -> tuple[bool, str]:
    """Schema + cross-reference validation."""
    required = ["m", "home_score", "away_score"]
    for k in required:
        if k not in m:
            return False, f"missing {k}"
    if not isinstance(m["m"], int):
        return False, "invalid match id (not int)"
    if not isinstance(m["home_score"], int) or m["home_score"] < 0:
        return False, "invalid home_score"
    if not isinstance(m["away_score"], int) or m["away_score"] < 0:
        return False, "invalid away_score"
    if m["home_score"] > 30 or m["away_score"] > 30:
        return False, "implausible scoreline (>30 goals)"
    fixture = next((f for f in schedule if f["m"] == m["m"]), None)
    if not fixture:
        return False, f"match {m['m']} not in WC2026 schedule"
    if m.get("home") and m["home"] != fixture["home"]:
        return False, f"home mismatch: expected {fixture['home']}, got {m['home']}"
    if m.get("away") and m["away"] != fixture["away"]:
        return False, f"away mismatch: expected {fixture['away']}, got {m['away']}"
    return True, "ok"


def atomic_write_json(path: Path, payload: dict):
    """Write JSON to a tmp file then rename — readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def main() -> int:
    src = os.environ.get("WC_RESULTS_SOURCE", "mock")
    print(f"[fetch_results] source={src}")

    try:
        cfg = json.loads((RAW / "wc2026_config.json").read_text())
    except Exception as e:
        print(f"[fetch_results] FATAL: cannot read wc2026_config.json — {e}")
        return 1
    schedule = cfg.get("group_stage_schedule", [])
    if not schedule:
        print("[fetch_results] FATAL: empty group_stage_schedule in config")
        return 1

    matches = []
    try:
        if src == "mock":
            matches = fetch_mock()
        elif src == "apifootball":
            key = os.environ.get("WC_APIFOOTBALL_KEY")
            if not key:
                print("[fetch_results] WC_APIFOOTBALL_KEY missing; falling back to mock")
                matches = fetch_mock()
            else:
                matches = fetch_apifootball(key)
        elif src == "sportmonks":
            token = os.environ.get("WC_SPORTMONKS_TOKEN")
            if not token:
                print("[fetch_results] WC_SPORTMONKS_TOKEN missing; falling back to mock")
                matches = fetch_mock()
            else:
                matches = fetch_sportmonks(token)
        else:
            print(f"[fetch_results] unknown source {src!r}; falling back to mock")
            matches = fetch_mock()
    except Exception as e:
        print(f"[fetch_results] adapter raised {type(e).__name__}: {e} — keeping existing file")
        return 0  # graceful no-op

    if not isinstance(matches, list):
        print(f"[fetch_results] adapter returned non-list ({type(matches).__name__}); keeping existing file")
        return 0

    # Validate + dedupe + categorize
    seen_m = set()
    valid = []
    rejected = []
    warnings = []  # postponed/abandoned/etc.
    for m in matches:
        if not isinstance(m, dict):
            rejected.append(({"m": "?"}, f"non-dict record ({type(m).__name__})"))
            continue
        status = (m.get("status") or "").upper()
        if status in WARN_STATUSES:
            warnings.append({"m": m.get("m", "?"), "status": status, "note": m.get("note", "")})
            continue
        if status and status not in LOCKED_STATUSES:
            # LIVE / SCHEDULED / etc — not final yet, silently skip
            continue
        if m.get("m") in seen_m:
            rejected.append((m, "duplicate match id"))
            continue
        try:
            ok, why = validate_match(m, schedule)
        except Exception as e:
            rejected.append((m, f"validator crashed: {type(e).__name__}: {e}"))
            continue
        if not ok:
            rejected.append((m, why))
            continue
        seen_m.add(m["m"])
        valid.append(m)

    print(f"[fetch_results] valid={len(valid)} rejected={len(rejected)} warnings={len(warnings)}")
    for m, why in rejected[:5]:
        print(f"  ✗ M{m.get('m', '?')}: {why}")
    for w in warnings[:5]:
        print(f"  ⚠ M{w['m']}: {w['status']} {('· ' + w['note']) if w['note'] else ''}")

    out_path = LIVE / "results_2026.json"
    # If we have zero valid + zero warnings AND we'd be overwriting an existing
    # populated file from a different source, refuse — keeps prior locked data.
    if not valid and not warnings and out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
            if existing.get("completed_matches"):
                print("[fetch_results] adapter returned nothing useful; preserving existing locked matches")
                return 0
        except Exception:
            pass  # corrupted; we'll overwrite below

    out = {
        "schema": "Completed WC 2026 matches — locked. Future matches are simulated.",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": src,
        "completed_matches": valid,
        "warnings": warnings,
    }
    try:
        atomic_write_json(out_path, out)
    except Exception as e:
        print(f"[fetch_results] FATAL: could not write {out_path} — {e}")
        return 1
    print(f"[fetch_results] wrote {out_path} ({len(valid)} matches locked, {len(warnings)} warnings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
