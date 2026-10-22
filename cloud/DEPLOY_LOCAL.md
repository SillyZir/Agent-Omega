# Omega monitor — local macOS deployment (launchd)

The daily PR monitor runs natively on this machine (2026-07-29 migration; the
Claude Code cloud routine is retired — see in-20). The monitoring logic is
unchanged: `monitor_fetch.py` (authenticated gh → public REST fallback →
fail-honest unreachable) wrapped by `monitor_run.py` (diff vs cached state,
dated summary with `Mode:` line, state update for fetched PRs only, commit +
push). Uses the local `gh` keyring auth and osxkeychain git credentials.

## One complete cycle (manual)
```
bash cloud/monitor_daily.sh          # full cycle: fetch, diff, summary, state, commit, push
bash cloud/monitor_daily.sh --no-git # test cycle without touching git
```
Exit codes: 0 = complete (any honest mode) · 1 = unreachable · 2 = internal error.

## Install (once)

**Step 0 — REQUIRED macOS permission (TCC).** The repo lives under
`~/Desktop`, which macOS privacy protection blocks for background launchd
jobs (`Operation not permitted`, exit 126 — observed 2026-07-29). Grant once:
System Settings → Privacy & Security → **Full Disk Access** → `+` →
press `Cmd+Shift+G`, enter `/bin/bash`, add it and toggle it ON.
Without this the scheduled run cannot read the repo. (Manual runs from a
terminal are unaffected — the terminal app holds the permission.)

```
mkdir -p logs/monitor
cp cloud/launchd/com.omega.cloud-monitor.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.omega.cloud-monitor.plist
```
Bootstrapping triggers an immediate run (RunAtLoad); if today already
succeeded it exits instantly (`--skip-if-done` guard).

## Schedule
launchd `StartCalendarInterval` — **daily 09:17 local time** (matches the
retired cloud schedule). launchd is preferred over cron on macOS: it fires a
missed calendar event on wake from sleep, and `RunAtLoad` + the skip guard
catch up a run missed while powered off (relevant: this machine has lost
power twice).

## Start / stop / status
```
launchctl kickstart -k gui/$(id -u)/com.omega.cloud-monitor   # run now
launchctl print gui/$(id -u)/com.omega.cloud-monitor          # status, last exit code, schedule
launchctl bootout gui/$(id -u)/com.omega.cloud-monitor        # stop + unload
# uninstall = bootout, then: rm ~/Library/LaunchAgents/com.omega.cloud-monitor.plist
```

## Logs (gitignored, timestamped)
- `logs/monitor/monitor.log` — every cycle, one timestamped line per step
- `logs/monitor/launchd.out.log` / `launchd.err.log` — raw launchd capture
- `logs/monitor/last_success` — date stamp of the last successful cycle

## Outputs (committed, unchanged from cloud era)
- `cloud/summaries/YYYY-MM-DD.md` — daily summary, `Mode:` line near top
- `cloud/monitor_state.json` — cached PR state; FETCHED PRs updated only,
  untouched entirely on `unreachable`
- commit message is exactly `cloud-monitor: daily summary YYYY-MM-DD`
  (single line, no PR identifiers — GitHub back-reference rule)

## Verification
1. `bash cloud/monitor_daily.sh` → exit 0, log line `fetch mode=authenticated
   fetched=7 errors=0`, summary committed + pushed.
2. Spot-check the summary table vs `gh pr view <n> --repo <owner/repo>`.
3. State-preservation regression: `GITHUB_API_URL=https://127.0.0.1:9
   bash cloud/monitor_daily.sh --force-public --no-git` → exit 1, summary says
   unreachable, `cloud/monitor_state.json` checksum unchanged.
4. `launchctl print gui/$(id -u)/com.omega.cloud-monitor` → shows the 09:17
   calendar trigger and last exit status 0.
5. Standing daily proof: each morning's dated summary commit.

## Recovery after reboot / power loss
Nothing to do. The LaunchAgent re-bootstraps at login; a 09:17 run missed
while asleep fires on wake; a run missed while powered off fires at next
login (RunAtLoad) and self-skips if the day already succeeded. A run
interrupted mid-cycle is safe to repeat: state is written atomically and the
cycle is idempotent per day.

## Targets
`cloud/monitor_targets.json` remains the tracked-PR list — keep in sync with
`ops_tracked_prs.json` when PRs open/close (unchanged from the cloud era).
