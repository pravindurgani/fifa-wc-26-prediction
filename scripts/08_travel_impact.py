"""
08_travel_impact.py — Quantify the simulation impact of travel fatigue.

Compares predictions.json (travel on) vs predictions_no_travel.json (travel off).
Outputs: data/processed/travel_impact.json
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"


def main():
    on_path = PROC / "predictions.json"
    off_path = PROC / "predictions_no_travel.json"
    if not on_path.exists() or not off_path.exists():
        print("Need both predictions.json (travel on) and predictions_no_travel.json (off)")
        print("Run: python scripts/03_simulate.py --out predictions.json")
        print("Then: python scripts/03_simulate.py --no-travel --out predictions_no_travel.json")
        return

    on = json.loads(on_path.read_text())
    off = json.loads(off_path.read_text())

    on_p = {t["team"]: t["p_champion"] for t in on["team_predictions"]}
    off_p = {t["team"]: t["p_champion"] for t in off["team_predictions"]}

    diffs = []
    for team in on_p:
        diffs.append({
            "team": team,
            "p_champion_with_travel": on_p[team],
            "p_champion_no_travel": off_p.get(team, 0),
            "delta_pp": (on_p[team] - off_p.get(team, 0)) * 100,
            "delta_rel": (on_p[team] - off_p.get(team, 0)) / max(off_p.get(team, 0.001), 0.001),
        })
    diffs.sort(key=lambda d: -d["delta_pp"])

    # Compute per-team total travel km from match predictions
    team_travel = {}
    for m in on["match_predictions"]:
        team_travel.setdefault(m["home"], []).append(m.get("home_travel_km", 0))
        team_travel.setdefault(m["away"], []).append(m.get("away_travel_km", 0))
    total_travel = {t: sum(km for km in kms if km > 0) for t, kms in team_travel.items()}

    out = {
        "scenario_with_travel": {"n_sims": on["n_simulations_total"]},
        "scenario_no_travel": {"n_sims": off["n_simulations_total"]},
        "beneficiaries_top5": [d for d in diffs if d["delta_pp"] > 0][:5],
        "losers_top5": [d for d in diffs if d["delta_pp"] < 0][-5:][::-1],
        "all_diffs": diffs,
        "total_group_travel_km_by_team": dict(sorted(total_travel.items(), key=lambda kv: -kv[1])),
    }
    out_path = PROC / "travel_impact.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"[OK] {out_path}")

    print("\nTop-5 travel beneficiaries (less affected):")
    for d in out["beneficiaries_top5"]:
        print(f"  {d['team']:<22s} {d['p_champion_with_travel']*100:5.2f}% → {d['p_champion_no_travel']*100:5.2f}% (Δ {d['delta_pp']:+.2f}pp)")

    print("\nTop-5 travel losers (more affected):")
    for d in out["losers_top5"]:
        print(f"  {d['team']:<22s} {d['p_champion_with_travel']*100:5.2f}% → {d['p_champion_no_travel']*100:5.2f}% (Δ {d['delta_pp']:+.2f}pp)")

    print("\nTeams with most group-stage travel km:")
    for t, km in list(total_travel.items())[:5]:
        if km > 0:
            print(f"  {t:<22s} {km:.0f} km across 3 group matches")


if __name__ == "__main__":
    main()
