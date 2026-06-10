# FIFA World Cup 2026 — AI Prediction Dashboard (v3)

End-to-end probabilistic simulator for the 2026 World Cup. Survived three rounds
of independent expert review. Production-ready: light/dark theme, multi-seed CIs,
calibration audit, walk-forward backtests, sensitivity analysis, travel fatigue,
injury layer, live-mode foundation, pre-launch validation script.

**Live dashboard**: deploy with `vercel deploy` or push to GitHub Pages from `dashboard/`.

## What's in the box

| Layer | What it does |
|---|---|
| `scripts/01_prepare_data.py` | Ingest 49k international matches, normalize team names, compute Elo |
| `scripts/02_goal_model.py` | Train two XGBoost Poisson regressors (home/away goals) |
| `scripts/03_simulate.py` | Monte Carlo sim — Annex C bracket, NB+Dixon-Coles, travel, injuries, live mode |
| `scripts/04_evaluate.py` | Calibration + holdout backtest |
| `scripts/05_sensitivity.py` | 22-scenario sensitivity audit |
| `scripts/06_ablation.py` | Elo-only vs goal-model lift |
| `scripts/07_walk_forward.py` | Walk-forward backtest on WC 2010/14/18/22 |
| `scripts/08_travel_impact.py` | Diff travel-on vs travel-off, output travel_impact.json |
| `scripts/09_validate.py` | Pre-launch validator (versions, sims, JSON integrity, secrets) |
| `scripts/tiebreakers.py` | Full FIFA 2026 tiebreaker cascade |

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Full pipeline (~5 minutes total)
.venv/bin/python scripts/01_prepare_data.py
.venv/bin/python scripts/02_goal_model.py
.venv/bin/python scripts/04_evaluate.py
.venv/bin/python scripts/06_ablation.py
.venv/bin/python scripts/07_walk_forward.py
.venv/bin/python scripts/05_sensitivity.py
.venv/bin/python scripts/03_simulate.py --no-travel --out predictions_no_travel.json
.venv/bin/python scripts/03_simulate.py
.venv/bin/python scripts/08_travel_impact.py

# Copy artifacts to dashboard
for f in predictions calibration travel_impact; do cp data/processed/${f}.json dashboard/; done
for f in walk_forward ablation sensitivity; do cp models/${f}.json dashboard/; done

# Pre-launch validation — must pass before publishing
.venv/bin/python scripts/09_validate.py

# Serve locally
cd dashboard && python3 -m http.server 8765
# → http://localhost:8765
```

## CLI flags

```bash
python scripts/03_simulate.py --quick           # 3 seeds × 2k sims (smoke test, ~10s)
python scripts/03_simulate.py --seeds 5 --sims 5000   # production (~30s)
python scripts/03_simulate.py --no-travel       # disable travel fatigue (for impact diff)
python scripts/03_simulate.py --no-dispersion   # Poisson instead of Negative Binomial
python scripts/03_simulate.py --no-adjustments  # ignore injury layer
python scripts/03_simulate.py --live --out predictions_live.json   # live mode w/ locked completed matches
```

## Going live

**Option 1: Vercel (recommended)** — `vercel.json` provided.
```bash
npm i -g vercel
vercel deploy --prod
```

**Option 2: GitHub Pages** — push to a repo, enable Pages from `dashboard/`.

**Daily refresh**: `.github/workflows/refresh-predictions.yml` runs the full pipeline twice
a day, runs the validator, and uploads fresh JSON. Uncomment the Vercel deploy step and
add `VERCEL_TOKEN` / `VERCEL_ORG_ID` / `VERCEL_PROJECT_ID` secrets to auto-deploy.

## Live mode (during the tournament)

Edit `data/live/results_2026.json` to add completed match scorelines:

```json
{
  "completed_matches": [
    {"m": 1, "date": "2026-06-11", "home_score": 2, "away_score": 0},
    {"m": 2, "date": "2026-06-11", "home_score": 1, "away_score": 1}
  ]
}
```

Then `python scripts/03_simulate.py --live --out predictions_live.json` — locked matches
are used verbatim, future matches simulated with updated state.

## Injury / suspension layer

Edit `data/live/team_adjustments.json`:

```json
{
  "adjustments": [
    {"team": "France", "player": "Kylian Mbappé", "status": "out",
     "adjustment_elo": -30, "source": "manual", "expires_at": "2026-06-25T00:00:00Z"}
  ]
}
```

Adjustments apply at simulation time only; they don't permanently modify Elo.
Expired adjustments are ignored. Use `--no-adjustments` to ignore the whole file.

## Project layout

```
fifa-wc-26-prediction/
├── data/
│   ├── raw/
│   │   ├── results.csv                            # 49k matches since 1872
│   │   ├── wc2026_config.json                     # groups, schedule, venues, FIFA pts
│   │   ├── knockout_bracket_2026.json             # FIFA R32 → final structure
│   │   ├── annex_c_third_place_table_2026.json    # 495-row Annex C lookup
│   │   ├── tiebreakers_2026.json                  # FIFA 2026 tiebreaker rules
│   │   ├── squad_values_2026.json                 # Transfermarkt per team
│   │   ├── host_city_distance_matrix.json         # pre-computed 16×16 km matrix
│   │   └── host_city_distances.py                 # one-shot builder
│   ├── processed/                                  # parquet + JSON outputs
│   └── live/
│       ├── results_2026.json                      # completed match scorelines
│       └── team_adjustments.json                  # injury/suspension Elo bumps
├── scripts/                                        # see "What's in the box"
├── models/                                         # trained joblib + metrics JSON
├── dashboard/
│   ├── index.html                                  # main dashboard
│   ├── methodology.html                            # plain-English walkthrough
│   ├── app.js  +  styles.css  +  methodology.css
│   └── *.json                                      # all data files served statically
├── vercel.json                                     # static-host config
├── .github/workflows/refresh-predictions.yml       # daily pipeline + deploy
├── requirements.txt
└── README.md
```

## Model performance (v3)

| Metric                          | Value  | Baseline | Lift |
|---|---|---|---|
| Holdout log-loss                | 0.869  | Elo-only 0.908, naive 1.055 | +0.039 vs Elo |
| Holdout Brier                   | 0.511  | Elo-only 0.535            | +0.024 vs Elo |
| Holdout accuracy                | 60.2%  | always-home ≈ 48%         | +12pp |
| WC walk-forward avg log-loss    | 0.983  | naive 1.09                 | +0.10 honest |
| Calibration                     | ✓     | predicted 0.85 → actual 0.85 |  |
| Annex C lookup misses           | 0     | (target: 0 / 25,000+ sims) |  |

## Top contenders (latest run — 25,000 sims, 5 seeds × 5,000)

| # | Team       | Champion | 95% CI       | Reach SF | Elo  |
|---|---|---|---|---|---|
| 1 | Spain      | 24.9%    | [24.3, 25.8] | 49.0%    | 2209 |
| 2 | Argentina  | 20.4%    | [19.4, 21.3] | 39.5%    | 2174 |
| 3 | France     | 8.4%     | [8.2, 8.5]   | 26.9%    | 2116 |
| 4 | England    | 6.4%     | [6.0, 6.7]   | 21.0%    | 2081 |
| 5 | Brazil     | 5.4%     | [4.9, 5.9]   | 23.4%    | 2054 |
| 6 | Colombia   | 4.9%     | [4.8, 5.1]   | 18.9%    | 2049 |

Concentration: top-1 24.9%, top-2 45.3%, top-5 65.5%. Within bookmaker norms.

## Travel impact (group stage)

| Team        | KM travelled | Champion-prob Δ vs no travel |
|---|---|---|
| Czechia     | 4,544 km     | (mid-pack effect)            |
| South Africa| 3,943 km     | (mid-pack effect)            |
| Canada      | 3,357 km     | (mid-pack effect)            |
| Spain       | ~700 km      | **+1.35pp** (benefits from others' fatigue) |
| France      | varies       | **−0.93pp** (Group I travel) |

## Sensitivity audit (across 22 scenarios)

| Team       | Mean  | Min   | Max   | Range |
|---|---|---|---|---|
| Spain      | 25.0% | 23.2% | 28.2% | 5.0pp |
| Argentina  | 20.2% | 18.6% | 22.0% | 3.3pp |
| France     | 8.8%  | 8.0%  | 10.3% | 2.3pp |
| England    | 6.5%  | 6.0%  | 6.9%  | 0.9pp |

Top-6 rank ordering identical across all 22 scenarios. The model is robust.

## Known limitations

- **xG features**: deferred — international xG data is patchy pre-2017; would require throwing out 2 decades of training. May add as recent-form modifier later.
- **Live injury feed**: framework in place (`data/live/team_adjustments.json`), but currently manual. API-Football / Sportmonks integration is a future enhancement.
- **In-tournament momentum**: pre-tournament form is held mostly static across the 25k sims. Live mode locks completed scorelines but does not yet auto-update Elo mid-tournament (next phase).
- **Refereeing patterns**: not modeled.

## Data sources

- **Match history**: [martj42/international_results](https://github.com/martj42/international_results) (CC0) — every game since 1872
- **FIFA rankings**: April 2026 release (next update 11 Jun 2026)
- **2026 schedule + bracket**: FIFA.com (final draw 5 Dec 2025), Annex C regulations
- **Squad values**: Transfermarkt via Sportingpedia / GiveMeSport
- **Stadium metadata**: hand-curated host city coordinates, altitude, climate

## License

Code under MIT. Data under their respective licenses.
