"""
01_prepare_data.py — Clean historical international match data, normalize country
names so they match the 2026 World Cup config, and compute Elo ratings up through
the most recent match. Outputs:

  data/processed/matches_clean.parquet  — cleaned match history
  data/processed/elo_ratings.json       — current Elo per nation
  data/processed/elo_history.parquet    — Elo trajectory per nation over time
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
PROC = ROOT / "data" / "processed"
PROC.mkdir(parents=True, exist_ok=True)

# ---------- Country-name normalization ----------------------------------------
# Map common alternate spellings from the historical dataset to the names we use
# in wc2026_config.json. Keeps everything joinable.
NAME_MAP: dict[str, str] = {
    "United States": "United States",
    "USA": "United States",
    "Republic of Ireland": "Ireland",
    "Korea Republic": "South Korea",
    "Korea DPR": "North Korea",
    "Czech Republic": "Czechia",
    "Czechoslovakia": "Czechia",
    "Iran": "Iran",
    "IR Iran": "Iran",
    "Türkiye": "Turkey",
    "Turkey": "Turkey",
    "Côte d'Ivoire": "Ivory Coast",
    "Cote d'Ivoire": "Ivory Coast",
    "Ivory Coast": "Ivory Coast",
    "Cape Verde Islands": "Cape Verde",
    "Cape Verde": "Cape Verde",
    "DR Congo": "DR Congo",
    "Congo DR": "DR Congo",
    "Democratic Republic of the Congo": "DR Congo",
    "Curaçao": "Curacao",
    "Curacao": "Curacao",
    "Bosnia and Herzegovina": "Bosnia and Herzegovina",
    "Saudi Arabia": "Saudi Arabia",
    "New Zealand": "New Zealand",
    "South Africa": "South Africa",
    "Soviet Union": "Russia",
    "CIS": "Russia",
    "FR Yugoslavia": "Serbia",
    "Yugoslavia": "Serbia",
    "Serbia and Montenegro": "Serbia",
    "West Germany": "Germany",
    "East Germany": "Germany",
}


def normalize(name: str) -> str:
    return NAME_MAP.get(name, name)


# ---------- Elo computation ---------------------------------------------------
# A football-flavored Elo: K varies with match importance and we incorporate the
# goal difference multiplier the way Eloratings.net does it.
TOURNAMENT_K = {
    "Friendly": 20,
    "FIFA World Cup": 60,
    "FIFA World Cup qualification": 40,
    "UEFA Euro": 50,
    "UEFA Euro qualification": 35,
    "Copa América": 50,
    "African Cup of Nations": 50,
    "AFC Asian Cup": 50,
    "CONCACAF Gold Cup": 40,
    "UEFA Nations League": 40,
    "Confederations Cup": 40,
}
DEFAULT_K = 30
INITIAL_ELO = 1500.0


def margin_multiplier(goal_diff: int) -> float:
    """Eloratings.net-style multiplier — caps the influence of blowouts."""
    if goal_diff <= 1:
        return 1.0
    if goal_diff == 2:
        return 1.5
    if goal_diff == 3:
        return 1.75
    return 1.75 + (goal_diff - 3) / 8.0


def expected_score(rating_a: float, rating_b: float, home_advantage: float) -> float:
    return 1.0 / (1.0 + 10.0 ** (((rating_b) - (rating_a + home_advantage)) / 400.0))


def compute_elo(matches: pd.DataFrame, home_advantage: float = 65.0):
    """Walk through every match chronologically and update Elo per team.

    Returns (current_elo dict, history DataFrame).
    """
    elo: dict[str, float] = defaultdict(lambda: INITIAL_ELO)
    history_rows: list[dict] = []

    for row in matches.itertuples(index=False):
        h, a = row.home_team, row.away_team
        h_score, a_score = row.home_score, row.away_score
        if pd.isna(h_score) or pd.isna(a_score):
            continue

        k = TOURNAMENT_K.get(row.tournament, DEFAULT_K)
        ha = 0.0 if row.neutral else home_advantage

        rh, ra = elo[h], elo[a]
        eh = expected_score(rh, ra, ha)
        ea = 1.0 - eh

        if h_score > a_score:
            sh, sa = 1.0, 0.0
        elif h_score < a_score:
            sh, sa = 0.0, 1.0
        else:
            sh = sa = 0.5

        mm = margin_multiplier(int(abs(h_score - a_score)))
        delta_h = k * mm * (sh - eh)
        delta_a = k * mm * (sa - ea)

        elo[h] = rh + delta_h
        elo[a] = ra + delta_a

        history_rows.append({
            "date": row.date, "team": h, "elo_after": elo[h], "opp": a,
            "is_home": not row.neutral, "tournament": row.tournament,
        })
        history_rows.append({
            "date": row.date, "team": a, "elo_after": elo[a], "opp": h,
            "is_home": False, "tournament": row.tournament,
        })

    return dict(elo), pd.DataFrame(history_rows)


def main() -> None:
    print("[1/4] Loading historical matches…")
    df = pd.read_csv(RAW / "results.csv", parse_dates=["date"])
    print(f"      Loaded {len(df):,} matches from {df.date.min().date()} to {df.date.max().date()}")

    print("[2/4] Normalizing country names…")
    df["home_team"] = df["home_team"].map(normalize)
    df["away_team"] = df["away_team"].map(normalize)
    df = df.sort_values("date").reset_index(drop=True)

    df.to_parquet(PROC / "matches_clean.parquet", index=False)

    print("[3/4] Computing Elo ratings over full history…")
    elo, hist = compute_elo(df)

    # Sort and save
    elo_sorted = dict(sorted(elo.items(), key=lambda kv: kv[1], reverse=True))
    (PROC / "elo_ratings.json").write_text(json.dumps(elo_sorted, indent=2))

    hist.to_parquet(PROC / "elo_history.parquet", index=False)

    print("[4/4] Top-20 Elo ratings:")
    for i, (team, rating) in enumerate(list(elo_sorted.items())[:20], 1):
        print(f"      {i:2d}. {team:<25s} {rating:7.1f}")

    # Sanity-check: every WC 2026 team must exist in our Elo table.
    cfg = json.loads((RAW / "wc2026_config.json").read_text())
    wc_teams = [t for grp in cfg["groups"].values() for t in grp]
    missing = [t for t in wc_teams if t not in elo]
    if missing:
        print(f"\n[WARN] WC 2026 teams missing from Elo: {missing}")
    else:
        print(f"\n[OK] All {len(wc_teams)} WC 2026 teams found in Elo ratings.")


if __name__ == "__main__":
    main()
