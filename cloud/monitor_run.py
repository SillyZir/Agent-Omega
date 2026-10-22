"""One complete monitoring cycle — native (launchd) replacement for the retired
cloud routine's execution layer. The monitoring logic is unchanged:
`monitor_fetch.py` is invoked as-is (subprocess, JSON on stdout) and this
script ports the routine's diff/summary/state rules deterministically.

Cycle: fetch -> diff vs monitor_state.json (notable events) -> write dated
summary with a Mode line -> update state for FETCHED PRs only -> commit ONLY
the summary + state -> push (failure non-fatal).

Fail-honest invariants (identical to the cloud routine):
- mode `unreachable` -> summary says so, monitor_state.json untouched.
- PRs in `errors` keep their cached state entries verbatim.
- Never fabricate; the summary reports exactly what the fetcher returned.
- PR names ONLY as 'owner/repo PR number' (GitHub back-reference rule); the
  commit message is EXACTLY 'cloud-monitor: daily summary YYYY-MM-DD'.

Flags: --skip-if-done (exit 0 if today already succeeded; used by the
launchd RunAtLoad catch-up), --no-git (test harness: skip pull/commit/push),
--force-public (passed through to the fetcher).

Exit codes: 0 = cycle complete (any honest mode), 1 = unreachable, 2 = error.
"""

import datetime
import json
import os
import subprocess
import sys

CLOUD = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(CLOUD)
STATE = os.path.join(CLOUD, "monitor_state.json")
TARGETS = os.path.join(CLOUD, "monitor_targets.json")
TRACKED = os.path.join(ROOT, "ops_tracked_prs.json")
SUMMARIES = os.path.join(CLOUD, "summaries")
LOGDIR = os.path.join(ROOT, "logs", "monitor")
LOGFILE = os.path.join(LOGDIR, "monitor.log")
MARKER = os.path.join(LOGDIR, "last_success")

US = "sillyzir"
_BOTS = ("claassistant", "cla-assistant", "codecov", "codecov-commenter",
         "github-actions", "dependabot", "netlify", "vercel", "sonarcloud")
AMBIENT_CI = ("caddyserver/caddy", "rclone/rclone")


def log(msg):
    line = f"[{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}"
    print(line)
    with open(LOGFILE, "a") as f:
        f.write(line + "\n")


def _is_bot(login):
    lg = (login or "").lower()
    return "[bot]" in lg or lg.endswith("-app") or lg in _BOTS


def _is_signal(login):
    """A login whose activity counts as maintainer signal (not us, not a bot)."""
    lg = (login or "").lower()
    return bool(lg) and lg != US and not _is_bot(lg)


def _key(repo, num):
    return f"{repo} PR {num}"


def _failing(rollup):
    return sorted({c.get("name") or "" for c in rollup or []
                   if (c.get("conclusion") or "").upper() in ("FAILURE", "ERROR")} - {""})


def _entry(p):
    """State entry for a fetched PR — same schema as the cloud routine wrote,
    plus failing_checks so new CI failures can be diffed next run, and
    n_checks so 'CI never ran' is distinguishable from 'CI passed'."""
    comments = p.get("comments") or []
    return {
        "state": p.get("state"),
        "reviewDecision": p.get("reviewDecision") or "",
        "n_comments": len(comments),
        "n_reviews": len(p.get("reviews") or []),
        "last_comment_author": ((comments[-1].get("author") or {}).get("login")
                                if comments else None),
        "updatedAt": p.get("updatedAt"),
        "failing_checks": _failing(p.get("statusCheckRollup")),
        "n_checks": len(p.get("statusCheckRollup") or []),
    }


def diff_events(p, old):
    """Notable events for one fetched PR vs its cached entry (routine step 3)."""
    events, flags = [], []
    repo, num = p["repo"], p["number"]
    new = _entry(p)
    old = old or {}

    if new["state"] != old.get("state"):
        if new["state"] == "MERGED":
            events.append("MERGED"); flags.append("merge")
        elif new["state"] == "CLOSED":
            events.append("CLOSED"); flags.append("close")
        elif old.get("state"):
            events.append(f"state {old['state']} -> {new['state']}")

    # empty derived reviewDecision = not derivable -> never an event
    if new["reviewDecision"] and new["reviewDecision"] != old.get("reviewDecision"):
        events.append(f"reviewDecision {old.get('reviewDecision') or '(none)'} -> {new['reviewDecision']}")
        if new["reviewDecision"] == "CHANGES_REQUESTED":
            flags.append("requested-changes")
        elif new["reviewDecision"] == "APPROVED":
            flags.append("approval")

    if new["n_reviews"] > (old.get("n_reviews") or 0):
        # only reviews BEYOND the cached count, and only signal authors —
        # otherwise our own reply-reviews get misattributed to the latest
        # maintainer review and raise a false flag (in-21, cousin of in-3)
        fresh = [r for r in (p.get("reviews") or [])[old.get("n_reviews") or 0:]
                 if _is_signal((r.get("author") or {}).get("login"))]
        for r in fresh:
            events.append(f"new review by {(r.get('author') or {}).get('login')}: {r.get('state')}")

    if new["n_comments"] != (old.get("n_comments") or 0):
        # ANY new signal-author comment fires, not only the LAST one — a
        # trailing bot/self comment used to mask a maintainer's comment
        # forever (audit P2-j; mirror of the in-21 reviews fix above)
        for c in (p.get("comments") or [])[old.get("n_comments") or 0:]:
            author = (c.get("author") or {}).get("login")
            if _is_signal(author):
                events.append(f"new comment by {author}")

    # "no checks ran" and "every check passed" both produce an empty
    # failing_checks list — indistinguishable until 2026-08-05, when go-git
    # PR 2238 (34d) and rclone PR 9642 (15d) both turned out to be sitting at
    # GitHub's first-time-contributor "approve and run workflows" gate with
    # ZERO check runs. Silence read as health for weeks. Emit once per PR when
    # we first see an open PR with no checks at all.
    if (new["state"] == "OPEN" and new["n_checks"] == 0
            and (old.get("n_checks") is None or old["n_checks"] > 0)):
        events.append("no CI checks have ever run on this PR — a maintainer "
                      "may still need to approve the workflow run")
        flags.append("ci-never-ran")

    new_fails = sorted(set(new["failing_checks"]) - set(old.get("failing_checks") or []))
    if new_fails:
        note = " (ambient-CI calibration: red on untouched packages / tooling drift is not a patch signal)" \
            if repo in AMBIENT_CI else ""
        events.append(f"new failing check(s): {', '.join(new_fails)}{note}")
        flags.append("ci-failure")

    return events, flags, new


def reconcile_targets():
    """Union ops_tracked_prs.json into monitor_targets.json before fetching.

    The auto-ship path already keeps both files in sync (executor_run.py:
    sync_tracking), but operator ships wrote only ops_tracked_prs.json — so
    the four PRs opened by hand on 2026-08-03 were never fetched. That is a
    fail-open on inputs repo_health reads for CHANGES_REQUESTED and staleness,
    i.e. a PR could sit with requested changes without degrading its repo's
    limit. Reconciling here makes the divergence self-healing whichever path
    opened the PR. Fail-soft: an unreadable/garbage ledger must never stop the
    monitoring cycle — we then monitor the committed target list as-is.
    """
    try:
        targets = json.load(open(TARGETS))
        tracked = json.load(open(TRACKED))
        have = {(t["repo"], t["number"]) for t in targets["prs"]}
        added = []
        for t in tracked:
            k = (t["repo"], t["number"])
            if k in have:
                continue
            targets["prs"].append({"repo": k[0], "number": k[1]})
            have.add(k)
            added.append(_key(*k))
        if added:
            tmp = TARGETS + ".tmp"
            with open(tmp, "w") as f:
                json.dump(targets, f, indent=1)
                f.write("\n")
            os.replace(tmp, TARGETS)
            log(f"targets reconciled from ops_tracked_prs.json: "
                f"+{len(added)} ({', '.join(added)})")
    except Exception as e:
        log(f"targets reconcile skipped ({type(e).__name__}: {e}) — "
            "monitoring the committed target list as-is")


def write_summary(day, mode, rows, notable, unfetched):
    lines = [f"# Cloud Monitor Summary — {day}", "", f"Mode: {mode}", "",
             "## Tracked PRs", "",
             "| PR | State | Review Decision | Last updated |",
             "|----|-------|-----------------|--------------|"]
    lines += rows
    if unfetched:
        lines += ["", "## Not fetched (cached state preserved)", ""]
        lines += [f"- {u}" for u in unfetched]
    lines += ["", "## Notable Events", ""]
    lines += notable if notable else ["None — no notable maintainer activity."]
    lines.append("")
    path = os.path.join(SUMMARIES, f"{day}.md")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def git_publish(day, no_git):
    if no_git:
        log("git: skipped (--no-git)")
        return
    def run(*args, fatal=False):
        p = subprocess.run(["git", "-C", ROOT, *args], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=120)
        if p.returncode != 0:
            msg = f"git {' '.join(args)} failed: {(p.stderr or p.stdout).strip()[:200]}"
            if fatal:
                raise RuntimeError(msg)
            log(msg + " (non-fatal)")
        return p
    # --autostash (audit P1-3): the chronic daily skip was a dirty tree
    # refusing plain --rebase; autostash rebases cleanly around local
    # modifications instead of silently not integrating origin
    run("pull", "--rebase", "--autostash", "origin", "main")
    # audit P2-i: a conflicted autostash APPLICATION leaves the changes in
    # the stash silently — detect and shout, never strand quietly
    _st = run("stash", "list")
    if "autostash" in ((_st.stdout or "") if hasattr(_st, "stdout") else ""):
        log("AUTOSTASH STRANDED after pull --rebase — local changes are "
            "parked in `git stash`; operator: inspect + `git stash pop` "
            "(audit P2-i)")
    # audit: scope the COMMIT to the same paths as the add. A pathless
    # `git commit` commits the WHOLE index, so a stale index makes this sweep
    # unrelated state — on 2026-09-12 it reverted b7da1f7. Idiom as
    # monitor_daily.sh:83.
    _paths = [os.path.join(CLOUD, f"summaries/{day}.md"), STATE, TARGETS]
    run("add", *_paths, fatal=True)
    c = run("commit", "-m", f"cloud-monitor: daily summary {day}", "--", *_paths)
    if c.returncode == 0:
        run("push")
        log("git: summary + state committed and pushed")
    else:
        log("git: nothing new to commit")


def main(argv):
    os.makedirs(LOGDIR, exist_ok=True)
    day = datetime.date.today().isoformat()

    # Chain-guard (system audit P2-1): refuse a MANUAL run while the daily
    # chain is mid-flight — every race found in the 2026-09-01 system audit
    # required a second writer entering between chain stages. Chain-invoked
    # runs carry OMEGA_CHAIN_PID matching the marker; a dead marker pid is
    # ignored (stale-safe).
    _cpf = os.path.join(ROOT, "logs", "chain_running.pid")
    if os.path.exists(_cpf):
        try:
            _cpid = int(open(_cpf).read().strip() or "0")
            # pid must be POSITIVE: os.kill(0,0) signals our own process
            # group and always succeeds (audit P2-d)
            _chain_alive = _cpid > 0 and (os.kill(_cpid, 0) or True)
        except (ValueError, OSError):
            _chain_alive = False
        if _chain_alive and os.getenv("OMEGA_CHAIN_PID") != str(_cpid):
            log(f"daily chain is running (pid {_cpid}) — refusing manual "
                "run (audit P2-1); retry after the chain completes")
            return 1

    if "--skip-if-done" in argv:
        done = (os.path.exists(MARKER) and open(MARKER).read().strip() == day
                and os.path.exists(os.path.join(SUMMARIES, f"{day}.md")))
        if done:
            log(f"already ran successfully today ({day}) — skipping (catch-up guard)")
            return 0

    reconcile_targets()

    fetch_args = [sys.executable, "-B", os.path.join(CLOUD, "monitor_fetch.py")]
    if "--force-public" in argv:
        fetch_args.append("--force-public")
    log(f"cycle start ({day})")
    p = subprocess.run(fetch_args, capture_output=True, encoding="utf-8",
                       errors="replace", timeout=900)
    try:
        result = json.loads(p.stdout)
    except Exception:
        log(f"fetcher output unparseable (exit {p.returncode}): {(p.stderr or p.stdout).strip()[:300]}")
        return 2
    mode, prs, errors = result["mode"], result["prs"], result["errors"]
    log(f"fetch mode={mode} fetched={len(prs)} errors={len(errors)}")

    state_doc = json.load(open(STATE))
    cached = state_doc.get("state", {})

    if mode == "unreachable":
        rows = [f"| {k} | {v.get('state','?')} | {v.get('reviewDecision') or '—'} | "
                f"{(v.get('updatedAt') or '—')[:10]} |" for k, v in cached.items()]
        unfetched = [f"{e['repo']} PR {e['number']} — {e['error']}" for e in errors]
        write_summary(day, mode, rows, ["None — GitHub unreachable this run."], unfetched)
        git_publish(day, "--no-git" in argv)
        log("mode unreachable: summary written, monitor_state.json untouched")
        return 1

    rows, notable, flags = [], [], []
    for pr in prs:
        k = _key(pr["repo"], pr["number"])
        events, fl, new_entry = diff_events(pr, cached.get(k))
        cached[k] = new_entry
        flags += fl
        rows.append(f"| {k} | {new_entry['state']} | {new_entry['reviewDecision'] or '—'} | "
                    f"{(new_entry['updatedAt'] or '—')[:10]} |")
        notable += [f"- {k}: {e}" for e in events]
    unfetched = [f"{e['repo']} PR {e['number']} — {e['error']}" for e in errors]
    for e in errors:  # cached entries for unfetched PRs stay verbatim; still list them
        k = _key(e["repo"], e["number"])
        if k in cached and all(not r.startswith(f"| {k} ") for r in rows):
            v = cached[k]
            rows.append(f"| {k} | {v.get('state','?')} | {v.get('reviewDecision') or '—'} | "
                        f"{(v.get('updatedAt') or '—')[:10]} |")

    # Summary BEFORE state (system audit P1-④, 2026-09-02): the old order
    # persisted the advanced event cursor first, so a kill or same-day rerun
    # between the two writes recomputed diffs against the NEW state and the
    # day's notable events — approvals, CHANGES_REQUESTED, merges — were
    # silently never reported. Summary-first flips the failure direction:
    # a kill in this window yields duplicate events on rerun (honest,
    # visible) instead of lost operator alerts.
    path = write_summary(day, mode, rows, notable, unfetched)
    state_doc.update({"last_run": day, "by": "local-monitor", "state": cached})
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state_doc, f, indent=1)
        f.write("\n")
    os.replace(tmp, STATE)

    git_publish(day, "--no-git" in argv)

    with open(MARKER, "w") as f:
        f.write(day + "\n")
    log(f"cycle complete: mode={mode}, {len(prs)}/{len(prs) + len(errors)} PRs, "
        f"{len(notable)} notable event(s){' — OPERATOR FLAGS: ' + ', '.join(sorted(set(flags))) if flags else ''}")
    log(f"summary: {os.path.relpath(path, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
