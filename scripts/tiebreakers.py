"""
tiebreakers.py — Official FIFA 2026 group-stage and best-third-placed tiebreakers.

Implements the cascade from
data/raw/tiebreakers_2026.json:

  Group ranking:
    1. points (all matches)
    2. goal difference (all matches)
    3. goals scored (all matches)
    4. head-to-head points among tied teams
    5. head-to-head goal difference
    6. head-to-head goals scored
    7. fair-play points (approximated as 0 in sim — no card data)
    8. FIFA Ranking points (latest)

  Best-third-placed ranking:
    1. points
    2. goal difference
    3. goals scored
    4. fair-play (skipped)
    5. FIFA Ranking points
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional


def _stats_from_matches(matches: list[dict], teams: list[str], scope_teams: Optional[set] = None) -> dict[str, dict]:
    """Aggregate stats. If scope_teams given, only count matches between those teams."""
    stats = {t: {"name": t, "pts": 0, "gd": 0, "gf": 0, "ga": 0, "played": 0} for t in teams}
    for m in matches:
        h, a = m["home"], m["away"]
        if scope_teams is not None and (h not in scope_teams or a not in scope_teams):
            continue
        hs, as_ = m["home_score"], m["away_score"]
        if h in stats:
            stats[h]["played"] += 1
            stats[h]["gf"] += hs
            stats[h]["ga"] += as_
            stats[h]["gd"] += hs - as_
        if a in stats:
            stats[a]["played"] += 1
            stats[a]["gf"] += as_
            stats[a]["ga"] += hs
            stats[a]["gd"] += as_ - hs
        if hs > as_:
            if h in stats:
                stats[h]["pts"] += 3
        elif hs < as_:
            if a in stats:
                stats[a]["pts"] += 3
        else:
            if h in stats:
                stats[h]["pts"] += 1
            if a in stats:
                stats[a]["pts"] += 1
    return stats


def _rank_by(stats: list[dict], keys: list[str]) -> list[list[dict]]:
    """Group teams by tuple of keys (descending). Returns list of equal-rank buckets."""
    def kf(s):
        return tuple(-s[k] for k in keys)
    sorted_stats = sorted(stats, key=kf)
    buckets = []
    cur = []
    cur_key = None
    for s in sorted_stats:
        k = kf(s)
        if k != cur_key:
            if cur:
                buckets.append(cur)
            cur = [s]
            cur_key = k
        else:
            cur.append(s)
    if cur:
        buckets.append(cur)
    return buckets


def rank_group(group_teams: list[str], group_matches: list[dict],
               fifa_points: dict[str, float]) -> list[dict]:
    """Return teams ordered 1st..4th per FIFA 2026 group-stage tiebreaker cascade.

    group_matches: list of {home, away, home_score, away_score} dicts (all 6 group matches).
    fifa_points: {team: float} — FIFA ranking points, used as ultimate tiebreaker.
    """
    overall = _stats_from_matches(group_matches, group_teams)
    # Step 1-3: pts, GD, GF over all matches
    buckets = _rank_by(list(overall.values()), ["pts", "gd", "gf"])

    final_order: list[dict] = []
    for bucket in buckets:
        if len(bucket) == 1:
            final_order.append(bucket[0])
            continue

        tied_names = [s["name"] for s in bucket]
        tied_set = set(tied_names)

        # Steps 4-6: head-to-head pts/GD/GF among the still-tied teams
        h2h_stats = _stats_from_matches(group_matches, tied_names, scope_teams=tied_set)
        sub_buckets = _rank_by(list(h2h_stats.values()), ["pts", "gd", "gf"])

        for sub in sub_buckets:
            if len(sub) == 1:
                final_order.append(overall[sub[0]["name"]])
                continue
            # Step 7: fair_play points — approximated as 0 in simulation
            # Step 8: FIFA Ranking (latest)
            sub_sorted = sorted(sub, key=lambda s: -fifa_points.get(s["name"], 0))
            for s in sub_sorted:
                final_order.append(overall[s["name"]])

    # Annotate position 1..4
    for pos, s in enumerate(final_order, start=1):
        s["pos"] = pos
    return final_order


def rank_third_placed(thirds: list[dict], fifa_points: dict[str, float]) -> list[dict]:
    """Rank the 12 third-placed teams; return top 8 (qualifiers) first, then bottom 4.

    Each item in thirds expected to have: name, pts, gd, gf, group
    """
    # Steps 1-3: pts, GD, GF
    buckets = _rank_by(thirds, ["pts", "gd", "gf"])
    ordered: list[dict] = []
    for bucket in buckets:
        if len(bucket) == 1:
            ordered.append(bucket[0])
        else:
            # Step 4: fair_play (skipped — 0)
            # Step 5: FIFA Ranking
            sub_sorted = sorted(bucket, key=lambda s: -fifa_points.get(s["name"], 0))
            ordered.extend(sub_sorted)
    return ordered


# ---- self-test --------------------------------------------------------------
if __name__ == "__main__":
    teams = ["A", "B", "C", "D"]
    matches = [
        {"home": "A", "away": "B", "home_score": 2, "away_score": 1},
        {"home": "C", "away": "D", "home_score": 1, "away_score": 0},
        {"home": "A", "away": "C", "home_score": 1, "away_score": 1},
        {"home": "B", "away": "D", "home_score": 2, "away_score": 0},
        {"home": "A", "away": "D", "home_score": 3, "away_score": 0},
        {"home": "B", "away": "C", "home_score": 1, "away_score": 2},
    ]
    fifa = {"A": 1800, "B": 1500, "C": 1600, "D": 1300}
    result = rank_group(teams, matches, fifa)
    for r in result:
        print(f"{r['pos']}. {r['name']}: {r['pts']}p GD={r['gd']:+d} GF={r['gf']}")
