"""
02_features_and_model.py — Build per-match features from historical results,
train an XGBoost classifier on home-win/draw/away-win, then save the model and
some interpretability artifacts.

Features per match (computed using ONLY data prior to match date):
  - elo_home, elo_away, elo_diff
  - rank_home, rank_away (FIFA-style derived from Elo at match time)
  - form_home_pts_last10, form_away_pts_last10  (points from last 10 matches)
  - gd_home_last10, gd_away_last10              (avg goal-diff last 10)
  - days_rest_home, days_rest_away              (days since previous match)
  - is_neutral
  - home_advantage_flag                         (host playing at home)
  - tournament_importance (0..1)
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import (accuracy_score, classification_report,
                             log_loss)
from sklearn.model_selection import train_test_split

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
MODELS = ROOT / "models"
MODELS.mkdir(parents=True, exist_ok=True)

TOURNAMENT_IMPORTANCE = {
    "Friendly": 0.10,
    "FIFA World Cup": 1.00,
    "FIFA World Cup qualification": 0.60,
    "UEFA Euro": 0.85,
    "UEFA Euro qualification": 0.55,
    "Copa América": 0.80,
    "African Cup of Nations": 0.75,
    "AFC Asian Cup": 0.70,
    "CONCACAF Gold Cup": 0.60,
    "UEFA Nations League": 0.55,
    "Confederations Cup": 0.65,
}


def label(home_score: int, away_score: int) -> int:
    """0 = away win, 1 = draw, 2 = home win."""
    if home_score > away_score:
        return 2
    if home_score < away_score:
        return 0
    return 1


def build_training_features(matches: pd.DataFrame, elo_path: Path) -> pd.DataFrame:
    """Build features chronologically, recomputing Elo as we go to avoid leakage."""
    from importlib import util
    spec = util.spec_from_file_location("p1", ROOT / "scripts" / "01_prepare_data.py")
    p1 = util.module_from_spec(spec)
    spec.loader.exec_module(p1)

    INITIAL_ELO = p1.INITIAL_ELO
    expected_score = p1.expected_score
    margin_multiplier = p1.margin_multiplier
    TOURNAMENT_K = p1.TOURNAMENT_K
    DEFAULT_K = p1.DEFAULT_K
    HOME_ADV = 65.0

    elo: dict[str, float] = defaultdict(lambda: INITIAL_ELO)
    last_matches: dict[str, deque] = defaultdict(lambda: deque(maxlen=10))
    last_date: dict[str, pd.Timestamp] = {}

    rows: list[dict] = []
    matches = matches.sort_values("date").reset_index(drop=True)

    for r in matches.itertuples(index=False):
        if pd.isna(r.home_score) or pd.isna(r.away_score):
            continue
        h, a = r.home_team, r.away_team
        date = r.date

        # ---- BUILD FEATURES (using state BEFORE this match) ----
        eh, ea_ = elo[h], elo[a]

        def form(team: str) -> tuple[float, float]:
            pts = 0
            gd = 0
            for m in last_matches[team]:
                pts += m["pts"]
                gd += m["gd"]
            n = max(1, len(last_matches[team]))
            return pts / n, gd / n

        f_h_pts, f_h_gd = form(h)
        f_a_pts, f_a_gd = form(a)

        rest_h = (date - last_date[h]).days if h in last_date else 30
        rest_a = (date - last_date[a]).days if a in last_date else 30

        importance = TOURNAMENT_IMPORTANCE.get(r.tournament, 0.30)

        rows.append({
            "date": date,
            "home_team": h, "away_team": a,
            "tournament": r.tournament, "neutral": bool(r.neutral),
            "elo_home": eh, "elo_away": ea_, "elo_diff": eh - ea_,
            "form_pts_home": f_h_pts, "form_pts_away": f_a_pts,
            "form_gd_home": f_h_gd, "form_gd_away": f_a_gd,
            "rest_home": min(rest_h, 60), "rest_away": min(rest_a, 60),
            "is_neutral": int(bool(r.neutral)),
            "importance": importance,
            "label": label(int(r.home_score), int(r.away_score)),
            "home_score": int(r.home_score), "away_score": int(r.away_score),
        })

        # ---- UPDATE STATE AFTER MATCH ----
        k = TOURNAMENT_K.get(r.tournament, DEFAULT_K)
        ha = 0.0 if r.neutral else HOME_ADV
        exp_h = expected_score(eh, ea_, ha)
        if r.home_score > r.away_score:
            sh, sa = 1.0, 0.0
            pts_h, pts_a = 3, 0
        elif r.home_score < r.away_score:
            sh, sa = 0.0, 1.0
            pts_h, pts_a = 0, 3
        else:
            sh = sa = 0.5
            pts_h = pts_a = 1
        mm = margin_multiplier(int(abs(r.home_score - r.away_score)))
        elo[h] = eh + k * mm * (sh - exp_h)
        elo[a] = ea_ + k * mm * (sa - (1 - exp_h))

        gd_h = int(r.home_score - r.away_score)
        last_matches[h].append({"pts": pts_h, "gd": gd_h})
        last_matches[a].append({"pts": pts_a, "gd": -gd_h})
        last_date[h] = date
        last_date[a] = date

    return pd.DataFrame(rows)


def main() -> None:
    print("[1/5] Loading clean matches…")
    df = pd.read_parquet(PROC / "matches_clean.parquet")

    print("[2/5] Building training features (this walks the full history)…")
    feats = build_training_features(df, PROC / "elo_ratings.json")
    feats.to_parquet(PROC / "match_features.parquet", index=False)
    print(f"      {len(feats):,} feature rows produced.")

    # Train only on relatively modern football — pre-1990 is structurally different.
    train_pool = feats[feats["date"] >= "1990-01-01"].copy()
    print(f"      Modern subset for training: {len(train_pool):,} matches.")

    FEATURE_COLS = [
        "elo_home", "elo_away", "elo_diff",
        "form_pts_home", "form_pts_away",
        "form_gd_home", "form_gd_away",
        "rest_home", "rest_away",
        "is_neutral", "importance",
    ]
    X = train_pool[FEATURE_COLS].values
    y = train_pool["label"].values

    # Time-based split — final 15% as test (most recent matches)
    cutoff = int(len(train_pool) * 0.85)
    X_tr, X_te = X[:cutoff], X[cutoff:]
    y_tr, y_te = y[:cutoff], y[cutoff:]

    print(f"[3/5] Training XGBoost classifier on {len(X_tr):,} rows…")
    clf = xgb.XGBClassifier(
        n_estimators=400,
        max_depth=5,
        learning_rate=0.05,
        objective="multi:softprob",
        num_class=3,
        eval_metric="mlogloss",
        tree_method="hist",
        random_state=42,
        n_jobs=-1,
    )
    clf.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], verbose=False)

    proba_te = clf.predict_proba(X_te)
    pred_te = proba_te.argmax(axis=1)

    acc = accuracy_score(y_te, pred_te)
    ll = log_loss(y_te, proba_te)
    print(f"[4/5] Holdout accuracy={acc:.3f}  log-loss={ll:.3f}")
    print(classification_report(y_te, pred_te, target_names=["away_win", "draw", "home_win"], digits=3))

    # Feature importance
    importances = dict(zip(FEATURE_COLS, clf.feature_importances_.tolist()))
    importances_sorted = dict(sorted(importances.items(), key=lambda kv: kv[1], reverse=True))
    print("[5/5] Feature importances:")
    for k, v in importances_sorted.items():
        print(f"      {k:<22s} {v:.4f}")

    joblib.dump(clf, MODELS / "wc_model.joblib")
    (MODELS / "feature_cols.json").write_text(json.dumps(FEATURE_COLS))
    (MODELS / "metrics.json").write_text(json.dumps({
        "accuracy": float(acc),
        "log_loss": float(ll),
        "n_train": int(len(X_tr)),
        "n_test": int(len(X_te)),
        "feature_importances": importances_sorted,
    }, indent=2))
    print(f"\n[OK] Model saved to {MODELS / 'wc_model.joblib'}")


if __name__ == "__main__":
    main()
