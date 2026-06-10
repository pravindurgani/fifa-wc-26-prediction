"""
fetch_results.py — Pluggable live-score fetcher.

Sources (selected via env var FOOTBALL_PROVIDER, with WC_RESULTS_SOURCE as a
backward-compatible alias):
  - mock (default)  — reads data/live/results_2026.json as-is (manual mode)
  - api_football    — live adapter for API-Football (requires API_FOOTBALL_KEY,
                      or WC_APIFOOTBALL_KEY for backward compatibility).
                      NOTE: API-Football FREE tier blocks 2026 — must be Pro+
  - football_data   — live adapter for football-data.org (requires
                      FOOTBALL_DATA_TOKEN). FREE TIER covers FIFA World Cup.
  - sportmonks      — placeholder adapter for Sportmonks (requires SPORTMONKS_TOKEN
                      or WC_SPORTMONKS_TOKEN)

Each adapter returns a list of normalised records:
  {
    "m": internal_match_id,
    "provider_fixture_id": str | None,
    "home": str, "away": str, "date": "YYYY-MM-DD",
    "home_score": int | None, "away_score": int | None,
    "status": "FT|AET|PEN|...",
    "status_long": "Match Finished|...",
    "source": "api_football|sportmonks|mock",
    "updated_at": ISO-8601,
  }

LOCK only when status ∈ {FT, AET, PEN}. POSTPONED/ABANDONED/CANCELED/SUSPENDED
are tracked as warnings — they never overwrite a locked result.

Hardening:
  - All HTTP calls wrapped in try/except + retries on transient 5xx.
  - Match-level validation wrapped per-record.
  - results_2026.json is written atomically.
  - If we'd be replacing N locked results with 0 (provider returned nothing),
    we refuse and preserve the existing file.

CLI:
  python scripts/live/fetch_results.py
  python scripts/live/fetch_results.py --provider api_football --dry-run
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "live"
RAW = ROOT / "data" / "raw"

# Module-level scratch list for fetch-time warnings (cleared at top of cmd_fetch
# / main). Provider fetchers append entries describing unmapped-yet-locked
# fixtures so the orchestrator can surface them in live_state.warnings instead
# of silently stalling at the last mapped match (e.g. when knockout fixture
# IDs land in the API before the provider_fixture_map.json is rebuilt). See
# main() — this list is merged into the persisted `warnings` array.
_FETCH_WARNINGS: list[dict] = []

LOCKED_STATUSES = {"FT", "AET", "PEN"}
WARN_STATUSES = {"POSTPONED", "ABANDONED", "CANCELED", "CANCELLED",
                 "SUSPENDED", "INTERRUPTED", "WALKOVER", "WALKOVERAWARD"}

# API-Football short codes → our internal canonical status
APIFOOTBALL_STATUS_MAP = {
    "FT":   "FT",          # Match Finished (regulation time)
    "AET":  "AET",         # After Extra Time
    "PEN":  "PEN",         # Penalty Shootout
    "PST":  "POSTPONED",
    "CANC": "CANCELED",
    "ABD":  "ABANDONED",
    "SUSP": "SUSPENDED",
    "INT":  "INTERRUPTED",
    "AWD":  "WALKOVERAWARD",
    "WO":   "WALKOVER",
    # In-progress / not-started — never lock these
    "TBD":  "SCHEDULED", "NS": "SCHEDULED",
    "1H":   "LIVE", "HT": "LIVE", "2H": "LIVE",
    "ET":   "LIVE", "BT": "LIVE", "P":  "LIVE", "LIVE": "LIVE",
}

# football-data.org status strings → our internal canonical status
# https://docs.football-data.org/general/v4/lookup_tables.html
FOOTBALLDATA_STATUS_MAP = {
    "SCHEDULED":   "SCHEDULED",
    "TIMED":       "SCHEDULED",
    "IN_PLAY":     "LIVE",
    "PAUSED":      "LIVE",
    "EXTRA_TIME":  "LIVE",
    "PENALTY_SHOOTOUT": "LIVE",
    "FINISHED":    "FT",         # generic full-time; AET/PEN inferred from score blocks
    "AWARDED":     "WALKOVERAWARD",
    "POSTPONED":   "POSTPONED",
    "SUSPENDED":   "SUSPENDED",
    "CANCELLED":   "CANCELED",
    "CANCELED":    "CANCELED",
}

# Team-name normalisation: provider name → our canonical name
TEAM_ALIAS = {
    "USA":                "United States",
    "U.S.A.":             "United States",
    "United States of America": "United States",
    "Korea Republic":     "South Korea",
    "Republic of Korea":  "South Korea",
    "Türkiye":            "Turkey",
    "Turkiye":            "Turkey",
    "Czech Republic":     "Czechia",
    "Cabo Verde":         "Cape Verde",
    "Cape Verde Islands": "Cape Verde",
    "Côte d'Ivoire":      "Ivory Coast",
    "Cote d'Ivoire":      "Ivory Coast",
    "Ivory Coast (Côte d'Ivoire)": "Ivory Coast",
    "IR Iran":            "Iran",
    "Iran Islamic Republic": "Iran",
    "Congo DR":           "DR Congo",
    "DR Congo":           "DR Congo",
    "Congo Democratic Republic": "DR Congo",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Curaçao":            "Curacao",
    "Saudi Arabia":       "Saudi Arabia",
    "New Zealand":        "New Zealand",
}


def normalize_team(name: str) -> str:
    """Map provider team name → our canonical name (used in wc2026_config)."""
    if not name:
        return name
    return TEAM_ALIAS.get(name.strip(), name.strip())


def get_provider_name() -> str:
    """Resolve provider from CLI/env. Order: FOOTBALL_PROVIDER, WC_RESULTS_SOURCE, 'mock'."""
    return (os.environ.get("FOOTBALL_PROVIDER")
            or os.environ.get("WC_RESULTS_SOURCE")
            or "mock").strip().lower().replace("-", "_")


def get_api_football_key() -> str | None:
    return (os.environ.get("API_FOOTBALL_KEY")
            or os.environ.get("WC_APIFOOTBALL_KEY"))


def get_sportmonks_token() -> str | None:
    return (os.environ.get("SPORTMONKS_TOKEN")
            or os.environ.get("WC_SPORTMONKS_TOKEN"))


def get_football_data_token() -> str | None:
    return (os.environ.get("FOOTBALL_DATA_TOKEN")
            or os.environ.get("WC_FOOTBALL_DATA_TOKEN"))


# ─── ATOMIC IO ─────────────────────────────────────────────────────────────
def _material_results(payload: dict) -> tuple:
    """Tick-to-tick material fingerprint of results_2026.json.

    Excludes the top-level `updated_at` AND each match's per-record
    `updated_at` so a no-op tick → byte-identical file → no git diff →
    no commit → no Vercel deploy. Mirrors the _material() pattern in
    run_live_update.write_live_state().

    Without this guard, the bare timestamp bump every 10 min during the
    14h match window = ~5,600 junk commits over 39 days. Live_state
    already has its own idempotency layer (924e20d); this completes the
    pair so the workflow's "Commit updated JSON" step can stay no-op
    on genuinely-quiet ticks.
    """
    def _scrub(m: dict) -> dict:
        return {k: v for k, v in m.items() if k != "updated_at"}
    matches = payload.get("completed_matches") or []
    return (
        payload.get("source"),
        json.dumps([_scrub(m) for m in matches], sort_keys=True),
        json.dumps(payload.get("warnings") or [], sort_keys=True),
    )


def _stamp_match_updated_at(
    candidate_matches: list[dict],
    prior_matches: list[dict] | None,
) -> list[dict]:
    """For each candidate match, reuse prior `updated_at` if material identical.

    Locked matches don't change once FT — their scoreline, status, and
    elapsed are frozen. Without this, every fetch overwrites each match's
    per-record updated_at with `now()`, defeating the outer idempotency
    guard because _material_results sees identical material but the inner
    timestamps differ if we compared them.

    We strip `updated_at` from the material fingerprint via _scrub() above,
    so this stamp-stability function is belt-and-braces — it ALSO keeps
    the on-disk bytes stable, which matters for human-readable diffs and
    for any downstream consumer that does dict-equality on the full record.
    """
    if not prior_matches:
        return candidate_matches
    prior_by_m = {m.get("m"): m for m in prior_matches if isinstance(m, dict)}

    def _match_material(m: dict) -> tuple:
        return (
            m.get("home_score"), m.get("away_score"), m.get("status"),
            m.get("status_long"), m.get("elapsed"), m.get("source"),
            m.get("raw_status"), m.get("date"), m.get("home"), m.get("away"),
        )

    out = []
    for cand in candidate_matches:
        prev = prior_by_m.get(cand.get("m"))
        if prev and _match_material(prev) == _match_material(cand):
            cand = dict(cand)
            cand["updated_at"] = prev.get("updated_at") or cand.get("updated_at")
        out.append(cand)
    return out


def atomic_write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def http_get_json(url: str, headers: dict, timeout: int = 15, retries: int = 3) -> dict:
    """HTTP GET with exponential backoff on 5xx + transient network errors.

    Raises urllib.error.HTTPError for 4xx (don't retry — likely auth/usage).
    """
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise  # don't retry client errors
            last_err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
        time.sleep(2 ** attempt)  # 1s, 2s, 4s
    raise last_err if last_err else RuntimeError(f"http_get_json failed: {url}")


# ─── PROVIDER: MOCK ─────────────────────────────────────────────────────────
def fetch_mock() -> list[dict]:
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


# ─── PROVIDER: API-FOOTBALL ────────────────────────────────────────────────
APIFOOTBALL_BASE = "https://v3.football.api-sports.io"


def load_fixture_map() -> dict | None:
    """Returns {provider_fixture_id_str: internal_match_id} if map file exists."""
    p = LIVE / "provider_fixture_map.json"
    if not p.exists():
        return None
    try:
        m = json.loads(p.read_text())
        out = {}
        for fx in m.get("fixtures", []):
            pfid = fx.get("provider_fixture_id")
            mid = fx.get("match_id") or fx.get("m")
            if pfid and mid:
                out[str(pfid)] = int(mid)
        return out
    except Exception as e:
        print(f"[fetch_results] WARN: provider_fixture_map.json unreadable: {e}")
        return None


def fetch_api_football(api_key: str, dry_run: bool = False) -> list[dict]:
    """Fetch WC2026 fixtures from API-Football v3, normalise to our schema."""
    headers = {"x-apisports-key": api_key, "Accept": "application/json"}

    # League + season come from the fixture map (preferred) or env (override)
    fix_map_file = LIVE / "provider_fixture_map.json"
    league_id = os.environ.get("API_FOOTBALL_LEAGUE_ID")
    season = os.environ.get("API_FOOTBALL_SEASON")
    if fix_map_file.exists():
        try:
            mf = json.loads(fix_map_file.read_text())
            league_id = league_id or mf.get("league_id")
            season = season or mf.get("season")
        except Exception:
            pass
    league_id = league_id or "1"   # API-Football's FIFA World Cup default
    season = season or "2026"

    url = f"{APIFOOTBALL_BASE}/fixtures?league={league_id}&season={season}"
    print(f"[fetch_results] GET {url}")

    try:
        payload = http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8")[:200]
        except Exception: pass
        print(f"[fetch_results] API-Football HTTP {e.code}: {body}")
        return []
    except Exception as e:
        print(f"[fetch_results] API-Football fetch failed: {type(e).__name__}: {e}")
        return []

    if payload.get("errors"):
        print(f"[fetch_results] API-Football returned errors: {payload['errors']}")
        # Don't return [] silently — surface upstream so we don't overwrite locked data
        if any(payload["errors"].values()):
            return []

    response = payload.get("response", []) or []
    print(f"[fetch_results] API-Football returned {len(response)} fixtures")

    if dry_run:
        # Print status distribution + next 5 + finals
        status_dist = {}
        finals, upcoming = [], []
        for f in response:
            s = (f.get("fixture", {}).get("status", {}) or {}).get("short", "?")
            status_dist[s] = status_dist.get(s, 0) + 1
            mapped = APIFOOTBALL_STATUS_MAP.get(s, s)
            if mapped in LOCKED_STATUSES: finals.append(f)
            elif mapped == "SCHEDULED": upcoming.append(f)
        print(f"[dry-run] status distribution: {status_dist}")
        print(f"[dry-run] finished: {len(finals)}, upcoming: {len(upcoming)}")
        for f in upcoming[:5]:
            home = (f.get("teams", {}).get("home", {}) or {}).get("name", "?")
            away = (f.get("teams", {}).get("away", {}) or {}).get("name", "?")
            date = (f.get("fixture", {}) or {}).get("date", "?")
            print(f"  upcoming: {date}  {home} vs {away}")
        for f in finals[:5]:
            home = (f.get("teams", {}).get("home", {}) or {}).get("name", "?")
            away = (f.get("teams", {}).get("away", {}) or {}).get("name", "?")
            gh = (f.get("goals", {}) or {}).get("home")
            ga = (f.get("goals", {}) or {}).get("away")
            print(f"  final: {home} {gh}-{ga} {away}")

    fixture_map = load_fixture_map() or {}
    if not fixture_map:
        print("[fetch_results] WARN: no provider_fixture_map.json — falling back to "
              "team+date matching. Run scripts/live/build_provider_fixture_map.py to "
              "create a deterministic map.")

    # Load our schedule for fuzzy fallback + date validation
    cfg = json.loads((RAW / "wc2026_config.json").read_text())
    schedule = cfg["group_stage_schedule"]
    schedule_by_id = {f["m"]: f for f in schedule}

    out = []
    unmapped = []
    for f in response:
        fx = f.get("fixture") or {}
        teams = f.get("teams") or {}
        goals = f.get("goals") or {}
        status = (fx.get("status") or {})
        short = status.get("short", "")
        canon_status = APIFOOTBALL_STATUS_MAP.get(short, short)
        provider_fixture_id = str(fx.get("id", ""))
        home_raw = (teams.get("home") or {}).get("name", "")
        away_raw = (teams.get("away") or {}).get("name", "")
        home = normalize_team(home_raw)
        away = normalize_team(away_raw)
        date = (fx.get("date") or "")[:10]  # ISO → YYYY-MM-DD

        # Resolve to our match id: prefer fixture map, fall back to (date±1, home, away)
        # ±1 day handles UTC↔local boundary (NA evening matches roll past midnight UTC)
        m_id = fixture_map.get(provider_fixture_id)
        if m_id is None:
            from datetime import date as _date, timedelta as _td
            try:
                d0 = _date.fromisoformat(date)
                date_window = {d0.isoformat(), (d0 - _td(days=1)).isoformat(),
                               (d0 + _td(days=1)).isoformat()}
            except Exception:
                date_window = {date}
            cand = next((s for s in schedule
                         if s["date"] in date_window
                         and s["home"] == home and s["away"] == away), None)
            if cand:
                m_id = cand["m"]
        if m_id is None:
            unmapped.append({"fixture_id": provider_fixture_id, "home": home_raw,
                             "away": away_raw, "date": date, "status": short})
            continue

        sched = schedule_by_id.get(m_id)
        if not sched:
            unmapped.append({"fixture_id": provider_fixture_id, "m": m_id, "reason": "m not in schedule"})
            continue

        gh = goals.get("home")
        ga = goals.get("away")
        if canon_status in LOCKED_STATUSES and (gh is None or ga is None):
            print(f"[fetch_results] WARN: M{m_id} status={short} but goals missing — skipping")
            continue

        out.append({
            "m": int(m_id),
            "provider_fixture_id": provider_fixture_id,
            "date": sched["date"],
            "home": sched["home"],
            "away": sched["away"],
            "home_score": int(gh) if isinstance(gh, int) else None,
            "away_score": int(ga) if isinstance(ga, int) else None,
            "status": canon_status,
            "status_long": status.get("long", ""),
            "elapsed": status.get("elapsed"),
            "source": "api_football",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "raw_status": short,
        })

    if unmapped:
        print(f"[fetch_results] {len(unmapped)} unmapped provider fixtures (likely friendlies)")
        for u in unmapped[:3]:
            print(f"  ? {u}")
        # Critical case: unmapped fixture is already LOCKED (FT/AET/PEN).
        # The map is missing a real WC match — likely a knockout fixture
        # that landed in the provider before provider_fixture_map.json was
        # rebuilt. Surface as a warning so the orchestrator can banner it.
        for u in unmapped:
            canon = APIFOOTBALL_STATUS_MAP.get(u.get("status", ""), u.get("status", ""))
            if canon in LOCKED_STATUSES:
                fxid = u.get("fixture_id", "?")
                home = u.get("home", "?")
                away = u.get("away", "?")
                dt = u.get("date", "?")
                _FETCH_WARNINGS.append({
                    "type": "unmapped_locked_fixture",
                    "message": (
                        f"Provider fixture {fxid} ({home} vs {away}, {dt}) "
                        "is locked but has no map entry — rebuild "
                        "data/live/provider_fixture_map.json"
                    ),
                })

    return out


# ─── PROVIDER: FOOTBALL-DATA.ORG ───────────────────────────────────────────
FOOTBALLDATA_BASE = "https://api.football-data.org/v4"


def fetch_football_data(token: str, dry_run: bool = False) -> list[dict]:
    """Fetch WC2026 fixtures from football-data.org, normalise to our schema.

    Free tier:
      - 10 requests/minute
      - FIFA World Cup competition code: "WC"
      - Endpoint: GET /v4/competitions/WC/matches
      - Auth header: X-Auth-Token
    """
    headers = {"X-Auth-Token": token, "Accept": "application/json"}
    competition = os.environ.get("FOOTBALL_DATA_COMPETITION") or "WC"
    url = f"{FOOTBALLDATA_BASE}/competitions/{competition}/matches"
    print(f"[fetch_results] GET {url}")

    try:
        payload = http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8")[:200]
        except Exception: pass
        print(f"[fetch_results] football-data.org HTTP {e.code}: {body}")
        return []
    except Exception as e:
        print(f"[fetch_results] football-data.org fetch failed: {type(e).__name__}: {e}")
        return []

    matches_raw = payload.get("matches", []) or []
    print(f"[fetch_results] football-data.org returned {len(matches_raw)} matches")

    if dry_run:
        status_dist: dict[str, int] = {}
        for m in matches_raw:
            s = m.get("status", "?")
            status_dist[s] = status_dist.get(s, 0) + 1
        print(f"[dry-run] status distribution: {status_dist}")
        for m in matches_raw[:5]:
            home = (m.get("homeTeam") or {}).get("name", "?")
            away = (m.get("awayTeam") or {}).get("name", "?")
            print(f"  fixture: {m.get('utcDate', '?')[:16]} {home} vs {away}  status={m.get('status')}")

    fixture_map = load_fixture_map() or {}
    cfg = json.loads((RAW / "wc2026_config.json").read_text())
    schedule = cfg["group_stage_schedule"]
    schedule_by_id = {f["m"]: f for f in schedule}

    out: list[dict] = []
    unmapped: list[dict] = []
    for m in matches_raw:
        provider_id = str(m.get("id", ""))
        home = normalize_team((m.get("homeTeam") or {}).get("name", ""))
        away = normalize_team((m.get("awayTeam") or {}).get("name", ""))
        date = (m.get("utcDate") or "")[:10]
        raw_status = m.get("status", "")
        canon_status = FOOTBALLDATA_STATUS_MAP.get(raw_status, raw_status)
        score = m.get("score") or {}
        full_time = score.get("fullTime") or {}
        extra_time = score.get("extraTime") or {}
        penalties = score.get("penalties") or {}

        # Resolve to our match id
        m_id = fixture_map.get(provider_id)
        if m_id is None:
            from datetime import date as _date, timedelta as _td
            try:
                d0 = _date.fromisoformat(date)
                date_window = {d0.isoformat(),
                               (d0 - _td(days=1)).isoformat(),
                               (d0 + _td(days=1)).isoformat()}
            except Exception:
                date_window = {date}
            cand = next((s for s in schedule
                         if s["date"] in date_window
                         and s["home"] == home and s["away"] == away), None)
            if cand:
                m_id = cand["m"]

        if m_id is None:
            unmapped.append({"id": provider_id, "home": home, "away": away,
                             "date": date, "status": raw_status})
            continue
        sched = schedule_by_id.get(m_id)
        if not sched:
            continue

        # Choose the right "final" goals: if AET/PEN was reached, use that;
        # otherwise plain fullTime.
        gh, ga = full_time.get("home"), full_time.get("away")
        # Distinguish AET / PEN by which sub-score is populated
        eff_status = canon_status
        if canon_status == "FT":
            if penalties.get("home") is not None or penalties.get("away") is not None:
                eff_status = "PEN"
            elif extra_time.get("home") is not None or extra_time.get("away") is not None:
                eff_status = "AET"
                # AET goals are commonly the cumulative score at end of ET → fullTime already holds it
        if eff_status in LOCKED_STATUSES and (gh is None or ga is None):
            print(f"[fetch_results] WARN: M{m_id} status={raw_status} but score missing — skipping")
            continue

        out.append({
            "m": int(m_id),
            "provider_fixture_id": provider_id,
            "date": sched["date"],
            "home": sched["home"],
            "away": sched["away"],
            "home_score": int(gh) if isinstance(gh, int) else None,
            "away_score": int(ga) if isinstance(ga, int) else None,
            "status": eff_status,
            "status_long": raw_status.replace("_", " ").title(),
            "elapsed": (m.get("minute") if isinstance(m.get("minute"), int) else None),
            "source": "football_data",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "raw_status": raw_status,
        })

    if unmapped:
        print(f"[fetch_results] {len(unmapped)} unmapped football-data fixtures (likely friendlies)")
        for u in unmapped[:3]:
            print(f"  ? {u}")
        # Same critical case for football-data.org: LOCKED unmapped fixtures
        # indicate a stale provider_fixture_map.json (commonly the post-R32
        # draw knockout IDs). Surface for the live_state banner.
        for u in unmapped:
            canon = FOOTBALLDATA_STATUS_MAP.get(u.get("status", ""), u.get("status", ""))
            if canon in LOCKED_STATUSES:
                fxid = u.get("id", "?")
                home = u.get("home", "?")
                away = u.get("away", "?")
                dt = u.get("date", "?")
                _FETCH_WARNINGS.append({
                    "type": "unmapped_locked_fixture",
                    "message": (
                        f"Provider fixture {fxid} ({home} vs {away}, {dt}) "
                        "is locked but has no map entry — rebuild "
                        "data/live/provider_fixture_map.json"
                    ),
                })

    return out


# ─── PROVIDER: SPORTMONKS (stub) ───────────────────────────────────────────
def fetch_sportmonks(token: str, dry_run: bool = False) -> list[dict]:
    """Placeholder for Sportmonks. See fetch_api_football for the pattern."""
    print("[fetch_results] sportmonks adapter not yet wired — returning empty list.")
    return []


# ─── VALIDATION ────────────────────────────────────────────────────────────
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


# ─── MAIN ──────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None,
                    help="mock | api_football | sportmonks (default: env)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch and print plan, but do not write results_2026.json")
    args = ap.parse_args()

    src = (args.provider or get_provider_name()).lower().replace("-", "_")
    print(f"[fetch_results] provider={src}{' (dry-run)' if args.dry_run else ''}")

    # Reset module-level fetch-time warnings so successive invocations don't
    # leak entries from a prior run (matters for tests + long-running orchestrators).
    _FETCH_WARNINGS.clear()

    try:
        cfg = json.loads((RAW / "wc2026_config.json").read_text())
    except Exception as e:
        print(f"[fetch_results] FATAL: cannot read wc2026_config.json — {e}")
        return 1
    schedule = cfg.get("group_stage_schedule", [])
    if not schedule:
        print("[fetch_results] FATAL: empty group_stage_schedule in config")
        return 1

    matches: list[dict] = []
    try:
        if src == "mock":
            matches = fetch_mock()
        elif src in ("api_football", "apifootball"):
            key = get_api_football_key()
            if not key:
                print("[fetch_results] API_FOOTBALL_KEY missing; falling back to mock")
                matches = fetch_mock()
                src = "mock"
            else:
                matches = fetch_api_football(key, dry_run=args.dry_run)
        elif src in ("football_data", "footballdata"):
            token = get_football_data_token()
            if not token:
                print("[fetch_results] FOOTBALL_DATA_TOKEN missing; falling back to mock")
                matches = fetch_mock()
                src = "mock"
            else:
                matches = fetch_football_data(token, dry_run=args.dry_run)
                src = "football_data"
        elif src == "sportmonks":
            token = get_sportmonks_token()
            if not token:
                print("[fetch_results] SPORTMONKS_TOKEN missing; falling back to mock")
                matches = fetch_mock()
                src = "mock"
            else:
                matches = fetch_sportmonks(token, dry_run=args.dry_run)
        else:
            print(f"[fetch_results] unknown provider {src!r}; falling back to mock")
            matches = fetch_mock()
            src = "mock"
    except Exception as e:
        print(f"[fetch_results] adapter raised {type(e).__name__}: {e} — keeping existing file")
        return 0  # graceful no-op

    if not isinstance(matches, list):
        print(f"[fetch_results] adapter returned non-list ({type(matches).__name__}); keeping existing file")
        return 0

    # Validate + dedupe + categorize
    seen_m = set()
    valid: list[dict] = []
    rejected: list[tuple[dict, str]] = []
    warnings_list: list[dict] = []
    for m in matches:
        if not isinstance(m, dict):
            rejected.append(({"m": "?"}, f"non-dict record ({type(m).__name__})"))
            continue
        status = (m.get("status") or "").upper()
        if status in WARN_STATUSES:
            warnings_list.append({"m": m.get("m", "?"), "status": status, "note": m.get("note", "")})
            continue
        if status and status not in LOCKED_STATUSES:
            continue  # SCHEDULED, LIVE — skip silently
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

    # Merge fetch-time warnings (e.g. unmapped_locked_fixture) so the
    # orchestrator can surface them via live_state.warnings.
    if _FETCH_WARNINGS:
        warnings_list.extend(_FETCH_WARNINGS)

    print(f"[fetch_results] valid={len(valid)} rejected={len(rejected)} warnings={len(warnings_list)}")
    for m, why in rejected[:5]:
        print(f"  ✗ M{m.get('m', '?')}: {why}")
    for w in warnings_list[:5]:
        if w.get("type") == "unmapped_locked_fixture":
            print(f"  ⚠ {w.get('message', '')}")
        else:
            print(f"  ⚠ M{w.get('m', '?')}: {w.get('status', '?')} {('· ' + w['note']) if w.get('note') else ''}")

    if args.dry_run:
        print("[fetch_results] dry-run — no file written")
        return 0

    out_path = LIVE / "results_2026.json"
    # Preserve existing locked data if provider returned nothing useful
    if not valid and not warnings_list and out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
            if existing.get("completed_matches"):
                print("[fetch_results] adapter returned nothing useful; preserving existing locked matches")
                return 0
        except Exception:
            pass
    # Also: if provider returned fewer locked matches than we already have, refuse —
    # likely a partial fetch or auth issue, not an actual rollback.
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
            existing_n = len(existing.get("completed_matches", []))
            if src != "mock" and len(valid) < existing_n:
                print(f"[fetch_results] provider returned {len(valid)} locked matches but "
                      f"existing has {existing_n}; refusing to shrink (preserving existing)")
                # Update warnings + source so the orchestrator sees freshness,
                # but PRESERVE updated_at if material is byte-identical
                # post-mutation. Stops shrink-refuse ticks from churning the
                # file (and triggering a deploy) when nothing genuine changed.
                prev_material = _material_results(existing)
                existing["warnings"] = warnings_list
                existing["source"] = src
                if _material_results(existing) != prev_material:
                    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
                atomic_write_json(out_path, existing)
                return 0
        except Exception:
            pass

    # Idempotency: preserve `updated_at` and per-match timestamps when material
    # fields are unchanged. Without this, every 10-min tick during the 14h
    # match window commits + deploys a timestamp-only diff → ~84 junk
    # commits/day → 100/day Vercel cap risk + ~3,000 commits over the
    # tournament. Mirrors the live_state.json idempotency pattern (924e20d).
    prior_payload = None
    if out_path.exists():
        try:
            prior_payload = json.loads(out_path.read_text())
        except Exception:
            prior_payload = None
    prior_matches = (prior_payload or {}).get("completed_matches") if prior_payload else None
    valid = _stamp_match_updated_at(valid, prior_matches)
    candidate = {"source": src, "completed_matches": valid, "warnings": warnings_list}
    if prior_payload and _material_results(prior_payload) == _material_results(candidate):
        ts = prior_payload.get("updated_at") or datetime.now(timezone.utc).isoformat()
    else:
        ts = datetime.now(timezone.utc).isoformat()
    out = {
        "schema": "Completed WC 2026 matches — locked. Future matches are simulated.",
        "updated_at": ts,
        "source": src,
        "completed_matches": valid,
        "warnings": warnings_list,
    }
    try:
        atomic_write_json(out_path, out)
    except Exception as e:
        print(f"[fetch_results] FATAL: could not write {out_path} — {e}")
        return 1
    print(f"[fetch_results] wrote {out_path} ({len(valid)} matches locked, "
          f"{len(warnings_list)} warnings, source={src})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
