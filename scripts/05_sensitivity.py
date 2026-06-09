"""
05_sensitivity.py — Sensitivity analysis on all hand-coded assumptions.

For each assumption, run the simulator with several values and report:
  - Champion probability range per top-12 team
  - Rank stability (how often is each team's rank stable across settings)
  - Biggest beneficiaries / losers

Saves: models/sensitivity.json
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
PROC = ROOT / "data" / "processed"
MODELS = ROOT / "models"
sys.path.insert(0, str(ROOT / "scripts"))

# Reuse simulator pieces
import importlib.util
spec = importlib.util.spec_from_file_location("sim", ROOT / "scripts" / "03_simulate.py")
sim = importlib.util.module_from_spec(spec); spec.loader.exec_module(sim)


def load_all():
    cfg_data = json.loads((RAW / "wc2026_config.json").read_text())
    bracket = json.loads((RAW / "knockout_bracket_2026.json").read_text())
    annex_c = json.loads((RAW / "annex_c_third_place_table_2026.json").read_text())
    squad_vals = json.loads((RAW / "squad_values_2026.json").read_text())["squad_values"]
    elo = json.loads((PROC / "elo_ratings.json").read_text())
    home_model = joblib.load(MODELS / "home_goals_model.joblib")
    away_model = joblib.load(MODELS / "away_goals_model.joblib")
    feature_cols = json.loads((MODELS / "feature_cols_v2.json").read_text())
    matches_df = pd.read_parquet(PROC / "matches_clean.parquet")
    return locals()


SCENARIOS = {
    "host_boost_home":  [20.0, 35.0, 50.0, 65.0, 80.0],
    "altitude_penalty_scale": [0.0, 10.0, 25.0, 40.0, 60.0],
    "heat_penalty":     [0.0, 7.5, 15.0, 25.0, 35.0],
    "squad_value_cap_override": [0.0, 10.0, 20.0, 40.0],
    "pen_elo_slope":    [400.0, 600.0, 800.0, 1000.0],
    "nb_dispersion":    [3.0, 5.0, 8.0, 15.0],
}


def run_scenario(cfg_overrides, n_sims=1500, ctx_cache=None, seeds=2):
    """Lightweight run for sensitivity — fewer sims per scenario, no per-match noise."""
    cfg = dict(sim.DEFAULTS)
    cfg["use_dispersion"] = True
    cfg["use_travel"] = False
    cfg["lambda_noise_per_match"] = False  # disable for speed in sensitivity
    cfg.update(cfg_overrides)

    if ctx_cache is None or ctx_cache.get("__cfg_key") != tuple(sorted(cfg.items())):
        d = load_all()
        ctx = sim.precompute_context(
            d["cfg_data"], d["bracket"], d["annex_c"], d["squad_vals"], d["elo"],
            d["home_model"], d["away_model"], d["feature_cols"], d["matches_df"], cfg)

    runs = []
    for seed in range(7, 7 + seeds):
        r = sim.run_single_seed(seed, cfg, n_sims, ctx)
        runs.append(r)
    per_team = sim.aggregate_runs(runs, ctx["all_teams"], n_sims)
    return per_team


def main():
    print("[1/3] Loading data + base context…")
    d = load_all()

    print(f"[2/3] Running sensitivity over {sum(len(v) for v in SCENARIOS.values())} scenarios…")
    results = {}
    cfg_default = dict(sim.DEFAULTS)
    cfg_default["use_dispersion"] = True
    cfg_default["use_travel"] = False
    cfg_default["lambda_noise_per_match"] = False
    base_ctx = sim.precompute_context(
        d["cfg_data"], d["bracket"], d["annex_c"], d["squad_vals"], d["elo"],
        d["home_model"], d["away_model"], d["feature_cols"], d["matches_df"], cfg_default)
    all_teams = base_ctx["all_teams"]

    for param, values in SCENARIOS.items():
        print(f"   Sensitivity on {param} ({values})…")
        results[param] = {}
        for v in values:
            cfg = dict(sim.DEFAULTS)
            cfg["use_dispersion"] = True; cfg["use_travel"] = False
            cfg["lambda_noise_per_match"] = False
            cfg[param] = v
            ctx = sim.precompute_context(
                d["cfg_data"], d["bracket"], d["annex_c"], d["squad_vals"], d["elo"],
                d["home_model"], d["away_model"], d["feature_cols"], d["matches_df"], cfg)
            runs = []
            for seed in (7, 11):
                runs.append(sim.run_single_seed(seed, cfg, 1500, ctx))
            per_team = sim.aggregate_runs(runs, all_teams, 1500)
            results[param][str(v)] = {
                t: per_team[t]["champion"]["mean"] for t in all_teams
            }
            print(f"      {param}={v}: top={max(per_team.items(), key=lambda kv: kv[1]['champion']['mean'])[0]} "
                  f"{max(p['champion']['mean'] for p in per_team.values())*100:.1f}%")

    # Compute rank stability and most-sensitive teams
    print("[3/3] Aggregating sensitivity…")
    summary = []
    for t in all_teams:
        # Across all scenario runs for all params, get champion probs
        all_p = []
        for param, sweep in results.items():
            for v, probs in sweep.items():
                all_p.append(probs[t])
        all_p = np.array(all_p)
        if all_p.mean() < 0.005:  # too rare to be meaningful
            continue
        summary.append({
            "team": t,
            "mean_champion_p": float(all_p.mean()),
            "min": float(all_p.min()), "max": float(all_p.max()),
            "range": float(all_p.max() - all_p.min()),
            "sensitivity_ratio": float((all_p.max() - all_p.min()) / max(all_p.mean(), 0.001)),
        })
    summary.sort(key=lambda r: -r["mean_champion_p"])

    print("\n   Top-12 team champion-probability ranges across all sensitivity scenarios:")
    for s in summary[:12]:
        print(f"   {s['team']:<22s} mean={s['mean_champion_p']*100:5.1f}%  "
              f"min={s['min']*100:5.1f}%  max={s['max']*100:5.1f}%  "
              f"range={s['range']*100:5.1f}pp  sens_ratio={s['sensitivity_ratio']:.2f}")

    out = {
        "scenarios": SCENARIOS,
        "results_per_team_per_setting": results,
        "summary_top12": summary[:12],
        "all_team_summary": summary,
    }
    (MODELS / "sensitivity.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"\n[OK] sensitivity.json written")


if __name__ == "__main__":
    main()
