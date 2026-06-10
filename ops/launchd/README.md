# WC26 launchd dispatcher

GitHub Actions cron is throttling our `*/10` schedule to zero ticks. This launchd
job runs `gh workflow run live-matchday.yml --ref main` every 10 minutes during
the tournament window, bypassing the broken scheduler entirely.

## Install (one-time, 60 seconds)

```bash
# 1. Stage files
mkdir -p ~/.config/wc26-dispatcher ~/Library/Logs ~/Library/LaunchAgents
cp ops/launchd/run.sh   ~/.config/wc26-dispatcher/run.sh
cp ops/launchd/com.pravindurgani.wc26-dispatch.plist ~/Library/LaunchAgents/

# 2. Write the env file containing your GitHub PAT
echo "GH_TOKEN=$(gh auth token)" > ~/.config/wc26-dispatcher/env

# 3. Lock down permissions (the run.sh refuses to read env unless 600)
chmod 700 ~/.config/wc26-dispatcher/run.sh
chmod 600 ~/.config/wc26-dispatcher/env

# 4. Load the launchd job
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.pravindurgani.wc26-dispatch.plist
```

## Verify

```bash
# Job is loaded
launchctl print gui/$UID/com.pravindurgani.wc26-dispatch | grep -E 'state|last exit|program'

# Manual kick (runs once, ignores the schedule)
launchctl kickstart -k gui/$UID/com.pravindurgani.wc26-dispatch

# Tail the log
tail -f ~/Library/Logs/wc26-dispatch.log
```

A successful tick logs three lines:

```
[2026-06-11T17:00:01Z] dispatching live-matchday.yml --ref main
✓ Created workflow_dispatch event for live-matchday.yml at main
[2026-06-11T17:00:02Z] ok
```

Outside the match window (06:00–15:59 UTC) the wrapper logs `skip — outside
match window` and exits 0. Outside the tournament dates (before 2026-06-10 or
after 2026-07-19) it logs `skip — outside tournament window`.

## Uninstall

```bash
launchctl bootout gui/$UID/com.pravindurgani.wc26-dispatch
rm ~/Library/LaunchAgents/com.pravindurgani.wc26-dispatch.plist
rm -r ~/.config/wc26-dispatcher
```

## Tuning

- **Want 24/7 ticks?** Comment out the hour-guard block in `run.sh` (lines
  marked "Hour guard").
- **Mac Mini sleeps mid-tournament?** Either `caffeinate -dimsu` it for the
  39-day window, or swap `StartInterval` for a `StartCalendarInterval` array
  with `WakeFromSleep: true`. `StartInterval=600` only fires while the system
  is awake.
- **Token rotation?** Re-run step 2: `echo "GH_TOKEN=$(gh auth token)" >
  ~/.config/wc26-dispatcher/env && chmod 600 ~/.config/wc26-dispatcher/env`.
  No launchctl reload needed — the wrapper reads the file fresh every tick.
- **Different log location?** Edit the two `StandardOutPath` / `StandardErrorPath`
  keys in the plist, then `launchctl bootout` + `bootstrap` to reload.
