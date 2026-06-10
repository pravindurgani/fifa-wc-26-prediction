#!/bin/bash
# WC26 dispatcher wrapper — fires `gh workflow run live-matchday.yml`
# against pravindurgani/fifa-wc-26-prediction:main when the UTC clock
# is inside a match window during the tournament dates.
#
# Why a wrapper instead of 84 StartCalendarInterval entries?
# - One readable file to edit if the window changes mid-tournament
# - Logs each tick (gated or fired) for trail-of-truth
# - Hour-gate is easy to comment out if you want 24/7 ticks
#
# Why GH_TOKEN from a file, not from keychain?
# - launchd doesn't unlock the keychain; `security` would prompt
# - `~/.config/wc26-dispatcher/env` is chmod 600, FileVault-protected
# - Token never appears in process args (we `source` it)
#
# Install:
#   mkdir -p ~/.config/wc26-dispatcher ~/Library/Logs
#   cp ops/launchd/run.sh ~/.config/wc26-dispatcher/run.sh
#   chmod 700 ~/.config/wc26-dispatcher/run.sh
#   echo "GH_TOKEN=$(gh auth token)" > ~/.config/wc26-dispatcher/env
#   chmod 600 ~/.config/wc26-dispatcher/env

set -uo pipefail

ENV_FILE="$HOME/.config/wc26-dispatcher/env"
LOG_PREFIX="[$(date -u +%FT%TZ)]"

# ─── Date guard: only during the WC 2026 window ────────────────────────────
DATE_UTC=$(date -u +%Y-%m-%d)
if [[ "$DATE_UTC" < "2026-06-10" ]] || [[ "$DATE_UTC" > "2026-07-19" ]]; then
  echo "$LOG_PREFIX skip — outside tournament window ($DATE_UTC)"
  exit 0
fi

# ─── Hour guard: 16:00 UTC through 05:59 UTC next day ──────────────────────
# Covers all WC 2026 kickoffs (18:00 BST = 17:00 UTC through to ~22:00 PT
# = ~05:00 UTC next day, with margin for FT + provider lag).
# Comment out this block if you want unconditional 24/7 ticks.
HOUR_UTC=$(date -u +%H)
HOUR_UTC=$((10#$HOUR_UTC))    # strip leading zero for arithmetic compare
if (( HOUR_UTC < 16 && HOUR_UTC >= 6 )); then
  echo "$LOG_PREFIX skip — outside match window ($HOUR_UTC:00 UTC)"
  exit 0
fi

# ─── Token guard ───────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
  echo "$LOG_PREFIX FAIL — $ENV_FILE missing; cannot dispatch"
  exit 1
fi
# Defensive perm check — if env file is world-readable, refuse to use it.
PERMS=$(stat -f "%Lp" "$ENV_FILE")
if [[ "$PERMS" != "600" ]]; then
  echo "$LOG_PREFIX FAIL — $ENV_FILE perms=$PERMS (need 600). chmod 600 it."
  exit 1
fi
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a
if [[ -z "${GH_TOKEN:-}" ]]; then
  echo "$LOG_PREFIX FAIL — GH_TOKEN not set in $ENV_FILE"
  exit 1
fi

# ─── Dispatch ──────────────────────────────────────────────────────────────
GH_BIN="/opt/homebrew/bin/gh"
[[ -x "$GH_BIN" ]] || GH_BIN="/usr/local/bin/gh"
[[ -x "$GH_BIN" ]] || { echo "$LOG_PREFIX FAIL — gh CLI not found"; exit 1; }

echo "$LOG_PREFIX dispatching live-matchday.yml --ref main"
if GH_TOKEN="$GH_TOKEN" "$GH_BIN" workflow run live-matchday.yml \
     -R pravindurgani/fifa-wc-26-prediction --ref main 2>&1; then
  echo "$LOG_PREFIX ok"
else
  rc=$?
  echo "$LOG_PREFIX FAIL rc=$rc"
  exit $rc
fi
