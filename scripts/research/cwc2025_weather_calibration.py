"""
cwc2025_weather_calibration.py — Stream B.6 reference / backtest.

The FIFA Club World Cup 2025 (15 June – 13 July 2025) was held in 11 US
cities, many of which overlap with the WC2026 host venues (NYC/NJ,
Los Angeles, Miami, Atlanta, Philadelphia, Charlotte, Cincinnati,
Nashville, Orlando, Pasadena, Seattle, Washington DC).

That makes CWC25 the best available calibration set for our v1
weather_adjustments table — same stadiums, same time of year, same
afternoon kickoffs that get the hottest conditions. This script:

  1. Reads a small hand-curated set of high-profile CWC25 matches
     (data/raw/cwc2025_calibration_matches.json — committed alongside
     this script) covering known hot venues.
  2. Calls Open-Meteo's historical archive endpoint for the actual
     match date/hour at each venue's coordinates.
  3. Runs the observed weather through the same classify_weather_bucket
     used in production.
  4. For each match, prints what Elo penalty each side WOULD have
     received under our current confederation table.

It does NOT modify any production state. The only output is a JSON
calibration report (data/raw/cwc2025_weather_calibration_report.json)
the operator can eyeball to decide whether v1 caps look right.

CLI:
  python3 scripts/research/cwc2025_weather_calibration.py
  python3 scripts/research/cwc2025_weather_calibration.py --dry-run
  python3 scripts/research/cwc2025_weather_calibration.py --no-network \
        --replay-file FIXTURE.json

Why this matters: if our table would have given UEFA clubs a -12 Elo
penalty in matches they actually won comfortably, that's a calibration
signal — either the table is too aggressive or other factors (squad
quality, motivation) dominate weather more than we assume. Conversely,
if matches where European clubs visibly wilted (e.g. PSG's heat-affected
games) don't trigger a hot-humid bucket, we're under-penalising.

References:
  - CWC25 schedule: https://www.fifa.com/clubworldcup
  - Open-Meteo Archive API: https://open-meteo.com/en/docs/historical-weather-api
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
CALIBRATION_MATCHES = RAW / "cwc2025_calibration_matches.json"
REPORT_OUT = RAW / "cwc2025_weather_calibration_report.json"

sys.path.insert(0, str(ROOT / "scripts" / "live"))
from weather_adjustments import (  # noqa: E402
    heat_index_c, wet_bulb_proxy_c,
    classify_weather_bucket, team_elo_adjustment,
)

OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def _http_get_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_historical_hour(lat: float, lon: float, date: str,
                          hour_utc: int) -> dict:
    """Pull one hour of historical weather. Returns the per-hour slice
    keyed by the requested hour, or {} on failure."""
    url = (
        f"{OPEN_METEO_ARCHIVE}?latitude={lat}&longitude={lon}"
        f"&start_date={date}&end_date={date}"
        f"&hourly=temperature_2m,relative_humidity_2m,precipitation,wind_gusts_10m"
        f"&timezone=UTC"
    )
    try:
        payload = _http_get_json(url)
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    target_prefix = f"{date}T{hour_utc:02d}:"
    for i, t in enumerate(times):
        if t.startswith(target_prefix):
            return {
                "time_utc": t,
                "temp_c": (hourly.get("temperature_2m") or [None])[i],
                "rh_pct": (hourly.get("relative_humidity_2m") or [None])[i],
                "precip_mm_per_h": (hourly.get("precipitation") or [None])[i],
                "wind_gust_kph": (hourly.get("wind_gusts_10m") or [None])[i],
            }
    return {"_error": "hour not found in response"}


def classify_match(match: dict, observation: dict) -> dict:
    """Run an observation through the production classifier + Elo table
    and return the would-be calibration entry."""
    temp_c = observation.get("temp_c")
    rh_pct = observation.get("rh_pct")
    precip = observation.get("precip_mm_per_h")
    wind = observation.get("wind_gust_kph")
    hi = heat_index_c(temp_c, rh_pct) if temp_c is not None else None
    wb = wet_bulb_proxy_c(temp_c, rh_pct) if temp_c is not None else None
    bucket = classify_weather_bucket(
        apparent_temp_c=hi,
        rh_pct=rh_pct,
        precip_mm_per_h=precip,
        wind_gust_kph=wind,
        temp_c=temp_c,
        wet_bulb_c=wb,
    )
    home_country = match.get("home_country")
    away_country = match.get("away_country")
    home_adj = (team_elo_adjustment(home_country, bucket)
                if home_country else 0.0)
    away_adj = (team_elo_adjustment(away_country, bucket)
                if away_country else 0.0)
    return {
        "match": match.get("label"),
        "venue": match.get("venue"),
        "date_local": match.get("date"),
        "kickoff_utc": observation.get("time_utc"),
        "temp_c": temp_c,
        "rh_pct": rh_pct,
        "precip_mm_per_h": precip,
        "wind_gust_kph": wind,
        "heat_index_c": round(hi, 1) if hi is not None else None,
        "wet_bulb_c": round(wb, 1) if wb is not None else None,
        "bucket": bucket,
        "home": home_country,
        "away": away_country,
        "home_elo_adjustment": home_adj,
        "away_elo_adjustment": away_adj,
        "observation_error": observation.get("_error"),
    }


def _bucket_counts(entries: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in entries:
        b = e.get("bucket") or "unknown"
        out[b] = out.get(b, 0) + 1
    return out


def _print_table(entries: list[dict]) -> None:
    """Compact stdout table — easier to eyeball than the JSON."""
    print()
    print(f"  {'Match':<42} {'Venue':<20} {'Bucket':<14} {'Home/Away Elo':<16}")
    print(f"  {'-'*42:<42} {'-'*20:<20} {'-'*14:<14} {'-'*16:<16}")
    for e in entries:
        label = (e.get("match") or "")[:42]
        venue = (e.get("venue") or "")[:20]
        bucket = (e.get("bucket") or "?")[:14]
        adj = f"{e['home_elo_adjustment']:+.0f}/{e['away_elo_adjustment']:+.0f}"
        print(f"  {label:<42} {venue:<20} {bucket:<14} {adj:<16}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="CWC2025 weather calibration backtest (Stream B.6).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't write the report JSON.")
    ap.add_argument("--no-network", action="store_true",
                    help="Use --replay-file instead of hitting Open-Meteo.")
    ap.add_argument("--replay-file", type=Path, default=None,
                    help="JSON list of pre-fetched observations (one per match).")
    args = ap.parse_args()

    if not CALIBRATION_MATCHES.exists():
        print(f"[calibration] missing {CALIBRATION_MATCHES} — see this file's docstring")
        return 2
    matches = json.loads(CALIBRATION_MATCHES.read_text())

    if args.no_network:
        if not args.replay_file or not args.replay_file.exists():
            print("[calibration] --no-network requires --replay-file FILE", file=sys.stderr)
            return 2
        observations = json.loads(args.replay_file.read_text())
    else:
        observations = []
        for m in matches:
            obs = fetch_historical_hour(
                m["lat"], m["lon"], m["date"], int(m.get("hour_utc", 19)))
            obs["_match_key"] = m["label"]
            observations.append(obs)

    # Align observations to matches by index (calibration set is small + ordered).
    entries = [classify_match(m, o) for m, o in zip(matches, observations)]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": 1,
        "n_matches": len(entries),
        "bucket_counts": _bucket_counts(entries),
        "entries": entries,
        "notes": (
            "Backtest: applies the live weather_adjustments table to "
            "FIFA Club World Cup 2025 matches at the actual hour of "
            "kickoff. Used to sanity-check the confederation×bucket Elo "
            "penalties before WC2026. NOT used by the production simulator."
        ),
    }
    _print_table(entries)
    print()
    print(f"  bucket counts: {report['bucket_counts']}")
    if args.dry_run:
        print(f"[calibration] dry-run — would write {REPORT_OUT.relative_to(ROOT)}")
        return 0
    REPORT_OUT.parent.mkdir(parents=True, exist_ok=True)
    REPORT_OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"[calibration] wrote {REPORT_OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
