"""
build_provider_fixture_map.py — One-shot fixture-id mapper.

Pulls World Cup 2026 fixtures from the configured provider, matches each to
your internal match id (wc2026_config.group_stage_schedule) by date + home +
away (with the same TEAM_ALIAS normalisation used at fetch time), and writes
data/live/provider_fixture_map.json.

After running this once, fetch_results.py uses the map for O(1) provider-id →
internal-id lookups — no more fuzzy matching on every live tick.

Usage:
    # Dry-run (no file written, prints what would map):
    python3 scripts/live/build_provider_fixture_map.py --provider api_football

    # Write the map (requires API_FOOTBALL_KEY in env):
    python3 scripts/live/build_provider_fixture_map.py --provider api_football --write

    # Override league/season (default: league=1, season=2026):
    python3 scripts/live/build_provider_fixture_map.py --provider api_football \\
        --league-id 1 --season 2026 --write
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "live"
RAW = ROOT / "data" / "raw"

# Reuse the adapter's normalisation + HTTP helper
sys.path.insert(0, str(Path(__file__).parent))
from fetch_results import (  # noqa: E402
    normalize_team, http_get_json, atomic_write_json,
    get_api_football_key, get_football_data_token,
    APIFOOTBALL_BASE, FOOTBALLDATA_BASE,
)


def fetch_apifootball_fixtures(api_key: str, league_id: str, season: str) -> list[dict]:
    headers = {"x-apisports-key": api_key, "Accept": "application/json"}
    url = f"{APIFOOTBALL_BASE}/fixtures?league={league_id}&season={season}"
    print(f"[builder] GET {url}")
    payload = http_get_json(url, headers)
    if payload.get("errors"):
        print(f"[builder] provider returned errors: {payload['errors']}")
        if any(payload["errors"].values()):
            return []
    return payload.get("response", []) or []


def fetch_football_data_fixtures(token: str, competition: str = "WC") -> list[dict]:
    """Returns football-data.org matches in their native shape (caller maps to internal)."""
    import urllib.error
    headers = {"X-Auth-Token": token, "Accept": "application/json"}
    url = f"{FOOTBALLDATA_BASE}/competitions/{competition}/matches"
    print(f"[builder] GET {url}")
    try:
        payload = http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8")[:300]
        except Exception: pass
        print(f"[builder] football-data.org HTTP {e.code}: {body}")
        if e.code == 400:
            print(f"[builder] HTTP 400 usually means: invalid token format, or the WC2026")
            print(f"[builder] competition isn't yet available in football-data.org's index.")
            print(f"[builder] Verify the token at https://api.football-data.org/v4/competitions")
            print(f"[builder] (curl -s -H 'X-Auth-Token: <YOUR_TOKEN>' that URL).")
        elif e.code in (401, 403):
            print(f"[builder] HTTP {e.code}: token is wrong/unauthorised. Double-check you")
            print(f"[builder] got the token from https://www.football-data.org/client/register")
            print(f"[builder] (NOT the API-Football key — they're different services).")
        elif e.code == 429:
            print(f"[builder] HTTP 429: rate-limited. Free tier is 10 req/min. Wait 60s.")
        raise
    return payload.get("matches", []) or []


def check_football_data_token(token: str) -> bool:
    """Hit /v4/competitions with the token. Returns True if 200 OK + WC is listed."""
    import urllib.error
    headers = {"X-Auth-Token": token, "Accept": "application/json"}
    url = f"{FOOTBALLDATA_BASE}/competitions"
    print(f"[builder] Verifying token via GET {url}")
    try:
        payload = http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8")[:300]
        except Exception: pass
        print(f"[builder] token check failed: HTTP {e.code} {body}")
        return False
    comps = payload.get("competitions") or []
    print(f"[builder] token is valid — {len(comps)} competitions visible")
    wc = [c for c in comps if c.get("code") == "WC" or (c.get("name") or "").startswith("FIFA World Cup")]
    if wc:
        print(f"[builder] ✓ FIFA World Cup IS available to this token "
              f"(code={wc[0].get('code')}, name={wc[0].get('name')}, "
              f"plan-tier={wc[0].get('plan', '?')})")
        return True
    else:
        print(f"[builder] ✗ FIFA World Cup is NOT in the competitions list for this token.")
        print(f"[builder]   Free tier should include it. Check your plan at "
              f"https://www.football-data.org/account.")
        sample = [c.get("code") for c in comps][:10]
        print(f"[builder]   Sample of available competitions: {sample}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="api_football",
                    choices=["api_football", "football_data", "sportmonks"])
    ap.add_argument("--league-id", default=None)
    ap.add_argument("--season", default="2026")
    ap.add_argument("--write", action="store_true",
                    help="Write the map. Without this flag, dry-run only.")
    ap.add_argument("--allow-partial", action="store_true",
                    help="Force-write even if fewer than 72 group fixtures mapped. "
                         "Use only if you know what you're doing.")
    ap.add_argument("--min-mapped", type=int, default=72,
                    help="Minimum mapped fixtures required to write (default: 72).")
    ap.add_argument("--check-token", action="store_true",
                    help="Only verify the provider token works (no map write). "
                         "Use this first when troubleshooting auth.")
    args = ap.parse_args()

    # --check-token shortcut
    if args.check_token:
        if args.provider == "football_data":
            token = get_football_data_token()
            if not token:
                print("[builder] FOOTBALL_DATA_TOKEN not set in env. Export it and re-run.")
                return 2
            return 0 if check_football_data_token(token) else 1
        elif args.provider == "api_football":
            key = get_api_football_key()
            if not key:
                print("[builder] API_FOOTBALL_KEY not set in env. Export it and re-run.")
                return 2
            try:
                payload = http_get_json(
                    f"{APIFOOTBALL_BASE}/status",
                    {"x-apisports-key": key, "Accept": "application/json"},
                )
                resp = payload.get("response") or {}
                acct = resp.get("account") or {}
                sub = resp.get("subscription") or {}
                print(f"[builder] API-Football token OK — account: "
                      f"{acct.get('firstname','?')} {acct.get('lastname','?')}, "
                      f"plan: {sub.get('plan','?')}, "
                      f"requests today: {(resp.get('requests') or {}).get('current','?')}/"
                      f"{(resp.get('requests') or {}).get('limit_day','?')}")
                if sub.get("plan", "").lower() == "free":
                    print(f"[builder] ⚠ FREE plan — 2026 WC fixtures will return empty. Upgrade to Pro/Ultra.")
                return 0
            except Exception as e:
                print(f"[builder] API-Football token check failed: {type(e).__name__}: {e}")
                return 1
        else:
            print(f"[builder] --check-token not implemented for {args.provider}")
            return 2

    cfg = json.loads((RAW / "wc2026_config.json").read_text())
    schedule = cfg["group_stage_schedule"]
    if len(schedule) != 72:
        print(f"[builder] FATAL: expected 72 fixtures in wc2026_config, got {len(schedule)}")
        return 1

    # Build a (home, away) -> [(date, match_id), ...] index so we can match
    # tolerantly across the UTC↔local-date boundary (NA evening matches roll
    # over to the next UTC day).
    from datetime import date as _date, timedelta as _td
    by_teams: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for s in schedule:
        by_teams.setdefault((s["home"], s["away"]), []).append((s["date"], s["m"]))

    def lookup(home: str, away: str, provider_date: str) -> int | None:
        candidates = by_teams.get((home, away), [])
        if not candidates:
            return None
        # Prefer the candidate whose date is closest to the provider date (within 1 day)
        try:
            target = _date.fromisoformat(provider_date)
        except Exception:
            return candidates[0][1]  # fall back to first
        best = None
        best_gap = 999
        for sched_date, m_id in candidates:
            try:
                sd = _date.fromisoformat(sched_date)
                gap = abs((sd - target).days)
                if gap <= 1 and gap < best_gap:
                    best, best_gap = m_id, gap
            except Exception:
                continue
        return best

    league_id = args.league_id or "1"

    if args.provider == "api_football":
        key = get_api_football_key()
        if not key:
            print("[builder] API_FOOTBALL_KEY not set. Export it and re-run.")
            return 2
        try:
            fixtures_raw = fetch_apifootball_fixtures(key, league_id, args.season)
        except Exception as e:
            print(f"[builder] provider fetch failed: {type(e).__name__}: {e}")
            return 1
        # Normalise to a common shape: {id, home, away, date}
        fixtures = [{
            "id": str((f.get("fixture") or {}).get("id", "")),
            "home_raw": ((f.get("teams") or {}).get("home") or {}).get("name", ""),
            "away_raw": ((f.get("teams") or {}).get("away") or {}).get("name", ""),
            "date": ((f.get("fixture") or {}).get("date") or "")[:10],
        } for f in fixtures_raw]
    elif args.provider == "football_data":
        token = get_football_data_token()
        if not token:
            print("[builder] FOOTBALL_DATA_TOKEN not set. Export it and re-run.")
            return 2
        competition = os.environ.get("FOOTBALL_DATA_COMPETITION") or "WC"
        try:
            fixtures_raw = fetch_football_data_fixtures(token, competition)
        except Exception as e:
            print(f"[builder] provider fetch failed: {type(e).__name__}: {e}")
            return 1
        fixtures = [{
            "id": str(m.get("id", "")),
            "home_raw": (m.get("homeTeam") or {}).get("name", ""),
            "away_raw": (m.get("awayTeam") or {}).get("name", ""),
            "date": (m.get("utcDate") or "")[:10],
        } for m in fixtures_raw]
    else:
        print(f"[builder] {args.provider} adapter not yet implemented")
        return 2

    print(f"[builder] provider returned {len(fixtures)} fixtures")

    mapped: list[dict] = []
    unmapped_provider: list[dict] = []
    for f in fixtures:
        provider_id = f["id"]
        home = normalize_team(f["home_raw"])
        away = normalize_team(f["away_raw"])
        date = f["date"]

        m_id = lookup(home, away, date)
        if m_id is None:
            unmapped_provider.append({
                "provider_fixture_id": provider_id,
                "date": date, "home": home, "away": away,
                "raw_home": f["home_raw"], "raw_away": f["away_raw"],
            })
            continue
        mapped.append({
            "match_id": m_id,
            "provider_fixture_id": provider_id,
            "home": home, "away": away, "date": date,
        })

    # Which of our 72 are NOT covered?
    mapped_internal = {x["match_id"] for x in mapped}
    unmapped_internal = [s for s in schedule if s["m"] not in mapped_internal]

    print(f"[builder] mapped: {len(mapped)} / 72 group fixtures")
    print(f"[builder] provider fixtures we couldn't map: {len(unmapped_provider)}")
    print(f"[builder] internal fixtures still unmapped: {len(unmapped_internal)}")

    for u in unmapped_provider[:5]:
        print(f"  ? provider: {u['date']} {u['home']} vs {u['away']} "
              f"(raw: {u['raw_home']!r} vs {u['raw_away']!r})")
    for s in unmapped_internal[:5]:
        print(f"  ? internal: M{s['m']} {s['date']} {s['home']} vs {s['away']}")

    if len(mapped) < 72:
        print(f"[builder] WARN: only mapped {len(mapped)}/72 group fixtures. "
              "Check team aliases in fetch_results.TEAM_ALIAS.")
    else:
        print(f"[builder] ✓ all 72 group fixtures mapped")

    if not args.write:
        print("[builder] dry-run — no file written. Re-run with --write to commit.")
        return 0

    # Refuse to write a useless map unless explicitly overridden
    if len(mapped) < args.min_mapped and not args.allow_partial:
        print(f"\n[builder] REFUSING TO WRITE: only {len(mapped)}/{args.min_mapped} fixtures mapped.")
        print("[builder] A partial map is worse than no map — the fetcher's fuzzy fallback")
        print("[builder]   handles missing IDs gracefully, but a stub map with 0 entries")
        print("[builder]   will be treated as authoritative and cause every fixture to be unmapped.")
        print("[builder]")
        print("[builder] Likely causes:")
        print("[builder]   • API-Football FREE plan blocks current/future seasons (2022–2024 only).")
        print("[builder]     → Upgrade to Pro/Ultra, OR switch to provider=football_data (free WC coverage).")
        print("[builder]   • Wrong league_id — pass --league-id <N> after checking your provider's WC league.")
        print("[builder]   • Wrong season — pass --season 2026 (default).")
        print("[builder]   • Provider uses a team name not in TEAM_ALIAS — add it to scripts/live/fetch_results.py.")
        print("[builder]")
        print(f"[builder] To force-write the partial map anyway: --allow-partial")
        return 1

    out = {
        "provider": args.provider,
        "league_id": league_id,
        "season": args.season,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixtures": sorted(mapped, key=lambda x: x["match_id"]),
        "unmapped_internal_count": len(unmapped_internal),
        "unmapped_provider_count": len(unmapped_provider),
    }
    out_path = LIVE / "provider_fixture_map.json"
    atomic_write_json(out_path, out)
    print(f"[builder] wrote {out_path}")
    if len(mapped) < 72:
        print(f"[builder] WARN: only {len(mapped)}/72 mapped — fetcher will fuzzy-match the rest")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
