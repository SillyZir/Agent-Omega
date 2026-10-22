"""One complete daily scouting cycle — repo scout + issue scout + ranking +
queue + report. Chained after the monitor in cloud/monitor_daily.sh (same
launchd job, 09:17 daily).

Phase-1 scope, by design: discovery/ranking/queueing/reporting ONLY. This
script never executes runner/pipeline_v2.py, never opens PRs, and removes no
safety gate — queued candidates in scout_queue.jsonl (gitignored, local-only)
await an operator-initiated pipeline run; repo proposals await an operator
edit of cloud/scout_config.json approved_repos.

Cycle: reachability probe -> repo scout (search/filter/score/propose) ->
issue scout over approved repos (hard filters -> confidence scoring ->
duplicate verification via agent/duplicate_check) -> append pipeline-ready
entries to scout_queue.jsonl (dedupe by issue key) -> update
cloud/scout_state.json -> write cloud/scout/YYYY-MM-DD.md -> commit report +
state -> success marker.

Fail-honest invariants (same discipline as monitor_run.py):
- unreachable -> report says so, state and queue untouched, exit 1, no marker.
- A repo whose fetch fails is reported under Errors and never scored/queued
  on partial data; prior state entries stay verbatim.
- Budget exhaustion degrades honestly: completed results reported, the rest
  listed as not examined.
- Committed artifacts name issues ONLY as 'owner/repo issue N' (GitHub
  back-reference rule); the pipeline-format key with '#' lives only in the
  gitignored queue file. Commit message is EXACTLY
  'cloud-scout: daily report YYYY-MM-DD'.

Flags: --skip-if-done (launchd catch-up guard), --no-git, --config <path>
(test override), --force-unreachable (test-only failure injection). Unknown
flags (the monitor's, passed through by the shared shell) are ignored.

Exit codes: 0 = cycle complete, 1 = unreachable, 2 = error.
"""

import datetime
import json
import os
import subprocess
import sys

CLOUD = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(CLOUD)
sys.path.insert(0, CLOUD)

from scout_gh import ApiBudget, BudgetExceeded, gh_reachable  # noqa: E402
import scout_repos  # noqa: E402
import scout_issues  # noqa: E402
import scout_history  # noqa: E402

CONFIG = os.path.join(CLOUD, "scout_config.json")
STATE = os.path.join(CLOUD, "scout_state.json")
REPORTS = os.path.join(CLOUD, "scout")
QUEUE = os.path.join(ROOT, "scout_queue.jsonl")
PIPELINE_QUEUE = os.path.join(ROOT, "pipeline_v2_queue.jsonl")
TRACKED = os.path.join(ROOT, "ops_tracked_prs.json")
MONITOR_TARGETS = os.path.join(CLOUD, "monitor_targets.json")
LOGDIR = os.path.join(ROOT, "logs", "scout")
LOGFILE = os.path.join(LOGDIR, "scout.log")
MARKER = os.path.join(LOGDIR, "last_success")


def log(msg):
    line = f"[{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}"
    print(line)
    with open(LOGFILE, "a") as f:
        f.write(line + "\n")


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"version": 1, "by": "local-scout",
            "key_format": "'<owner>/<repo> issue <number>' — never "
                          "'<owner>/<repo>#<number>' (avoids GitHub back-references)",
            "repos": {}, "issues": {}}


def save_state(state, day):
    state["last_run"] = day
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
        f.write("\n")
    os.replace(tmp, STATE)


def open_pr_counts():
    """Open tracked PRs per repo (concentration rule input). Prefers the
    monitor's state cache (knows OPEN vs MERGED/CLOSED); falls back to the
    tracked lists, conservatively counting every tracked PR as open."""
    monitor_state = os.path.join(CLOUD, "monitor_state.json")
    try:
        entries = json.load(open(monitor_state)).get("state", {})
        if entries:
            counts = {}
            for key, v in entries.items():
                if v.get("state") == "OPEN":
                    repo = key.split(" PR ")[0]
                    counts[repo] = counts.get(repo, 0) + 1
            return counts
    except Exception:
        pass
    for path in (TRACKED, MONITOR_TARGETS):
        try:
            doc = json.load(open(path))
            prs = doc["prs"] if isinstance(doc, dict) else doc
            counts = {}
            for pr in prs:
                counts[pr["repo"]] = counts.get(pr["repo"], 0) + 1
            return counts
        except Exception:
            continue
    return {}


def seen_issue_keys(state, cfg, today):
    """Safe keys the issue scout must skip: actively tracked in scout state,
    recently excluded (re-examined after reexamine_days), already queued, or
    already processed by the pipeline (read-only scan of the outcome log)."""
    seen = set()
    reexamine = cfg.get("reexamine_days", 30)
    for key, e in state.get("issues", {}).items():
        if e.get("status") in ("queued", "ranked"):
            seen.add(key)
        elif e.get("status") == "excluded":
            try:
                age = (today - datetime.date.fromisoformat(e["last_checked"])).days
            except Exception:
                age = 0
            if age < reexamine:
                seen.add(key)
    for path in (QUEUE, PIPELINE_QUEUE):
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    seen.add(f"{e['repo']} issue {e['number']}")
                except Exception:
                    continue
    return seen


def append_queue(candidates, day):
    """Append queued candidates in pipeline-ready form; dedupe by issue_key.
    Under the shared queue lock (audit P1-1): an executor rewriting the file
    concurrently would otherwise drop this append forever."""
    from queue_lock import QueueLock
    with QueueLock(QUEUE):
        return _append_queue_locked(candidates, day)


def _append_queue_locked(candidates, day):
    existing = set()
    if os.path.exists(QUEUE):
        with open(QUEUE) as f:
            for line in f:
                try:
                    existing.add(json.loads(line)["issue_key"])
                except Exception:
                    continue
    added = []
    with open(QUEUE, "a") as f:
        for c in candidates:
            if not c.get("queued"):
                continue
            issue_key = f"{c['repo']}#{c['number']}"
            if issue_key in existing:
                continue
            f.write(json.dumps({
                "queued_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "day": day, "source": "scout", "status": "pending",
                "repo": c["repo"], "number": c["number"], "title": c["title"],
                "body": c["body"], "issue_key": issue_key,
                "rank": c["rank"], "confidence": c["confidence"],
                "why": c["why"], "score_breakdown": c["breakdown"],
                "duplicate_check": c.get("duplicate_check"),
            }) + "\n")
            added.append(c["key"])
    return added


def write_report(day, mode, budget_line, repo_out, issue_out, queued_keys):
    lines = [f"# Daily Scout Report — {day}", "", f"Mode: {mode}",
             f"API budget: {budget_line}", "",
             "Note: discovery/ranking/queueing only — NO pipeline execution; "
             "human approval is required before any PR. Repo proposals require "
             "an operator edit of cloud/scout_config.json approved_repos.", ""]

    lines += ["## Repository Scout — proposals (operator action required)", ""]
    if repo_out.get("proposed"):
        lines += ["| repo | v2 score | v1 score | burdens | probe | why |",
                  "|------|----------|----------|---------|-------|-----|"]
        for p in repo_out["proposed"]:
            note = " (already profiled)" if p.get("known") else ""
            # Burdens are surfaced, never silently filtered: the operator sees
            # what contributing here will cost before approving the repo.
            burdens = ", ".join(f"{b['burden']} -{b['penalty']}"
                                for b in p.get("burdens", [])) or "none detected"
            lines.append(f"| {p['repo']}{note} | {p['score']} | "
                         f"{p.get('score_v1', 'n/a')} | {burdens} | "
                         f"{p.get('probe_status', 'n/a')} | {'; '.join(p['why'])} |")
        if any(p.get("burdens") for p in repo_out["proposed"]):
            lines += ["", "Burden evidence (verbatim excerpts from the repo's own docs):"]
            for p in repo_out["proposed"]:
                for b in p.get("burdens", []):
                    if b.get("evidence"):
                        lines.append(f"- {p['repo']} / {b['burden']}: \"{b['evidence']}\"")
        # Rendered as a bullet block BELOW the table rather than as a column:
        # scout_observe parses proposal rows by cell, so widening the table
        # would break the observer. Only repos we have actually contributed to
        # appear — the line's presence is itself the signal.
        prior = [(p["repo"], scout_history.summarize(p.get("history")))
                 for p in repo_out["proposed"]]
        prior = [(r, s) for r, s in prior if s]
        if prior:
            lines += ["", "Prior contribution record (ours, with this repo — "
                      "reported, never scored):"]
            # 'closed' alone reads as rejection. gnolang/gno was admitted while
            # carrying 5 closed PRs, 4 of which WE withdrew — the attribution
            # reversed the reading, so it is always shown with the count.
            lines += [f"- {r}: {s}" for r, s in prior]
    else:
        lines.append("None this run.")

    examined = repo_out.get("examined", [])
    lines += ["", "## Repository Scout — examined, not proposed", ""]
    if examined:
        lines += ["| repo | outcome | reason |", "|------|---------|--------|"]
        lines += [f"| {e['repo']} | {e['outcome']} | {e['reason']} |" for e in examined]
    else:
        lines.append("None.")

    lines += ["", "## Issue Scout — ranked candidates", ""]
    cands = issue_out.get("candidates", [])
    if cands:
        lines += ["| rank | issue | confidence | queued | why selected |",
                  "|------|-------|------------|--------|--------------|"]
        for c in cands:
            lines.append(f"| {c['rank']} | {c['key']} | {c['confidence']} | "
                         f"{'yes' if c['queued'] else 'no'} | {'; '.join(c['why'])} |")
    else:
        lines.append("None — no candidate cleared the confidence and duplicate gates.")

    # A reallocated slot is stated, never silent: the queue looks identical in
    # size either way, so without this line the swap is invisible.
    promo = issue_out.get("priority_promotion")
    if promo:
        lines += ["", f"Priority slot: queued **{promo['promoted']}** "
                  f"({promo['confidence']}) in the last slot, displacing "
                  f"{promo['displaced']} ({promo['displaced_confidence']}), "
                  f"which remains ranked for a later day. Slot count unchanged."]

    skipped = issue_out.get("repos_skipped", [])
    lines += ["", "## Repos skipped", ""]
    lines += ([f"- {s['repo']} — {s['reason']}" for s in skipped] or ["None."])

    excluded = issue_out.get("excluded", [])
    lines += ["", "## Excluded (negative results are data)", ""]
    if excluded:
        lines += ["| issue | reason |", "|-------|--------|"]
        lines += [f"| {e['key']} | {e['reason']} |" for e in excluded]
    else:
        lines.append("None.")

    errors = (repo_out.get("errors", []) + repo_out.get("probe_errors", [])
              + issue_out.get("errors", []))
    lines += ["", "## Errors / not examined", ""]
    lines += ([f"- {e}" for e in errors] or ["None."])
    lines.append("")

    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, f"{day}.md")
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
        run("pull", "--rebase", "--autostash", "origin", "main")
    # audit P2-i: a conflicted autostash APPLICATION leaves the changes in
    # the stash silently — detect and shout, never strand quietly
    _st = run("stash", "list")
    if "autostash" in ((_st.stdout or "") if hasattr(_st, "stdout") else ""):
        log("AUTOSTASH STRANDED after pull --rebase — local changes are "
            "parked in `git stash`; operator: inspect + `git stash pop` "
            "(audit P2-i)")
    # audit: scope the COMMIT to the same paths as the add (see
    # monitor_run.py:233) — a pathless commit publishes the whole index.
    _paths = [os.path.join(REPORTS, f"{day}.md"), STATE]
    run("add", *_paths, fatal=True)
    c = run("commit", "-m", f"cloud-scout: daily report {day}", "--", *_paths)
    if c.returncode == 0:
        run("push")
        log("git: report + state committed and pushed")
    else:
        log("git: nothing new to commit")


def arg_value(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def main(argv):
    os.makedirs(LOGDIR, exist_ok=True)
    today = datetime.date.today()

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
    day = today.isoformat()
    no_git = "--no-git" in argv

    if "--skip-if-done" in argv:
        done = (os.path.exists(MARKER) and open(MARKER).read().strip() == day
                and os.path.exists(os.path.join(REPORTS, f"{day}.md")))
        if done:
            log(f"scout already ran successfully today ({day}) — skipping (catch-up guard)")
            return 0

    cfg = json.load(open(arg_value(argv, "--config") or CONFIG))
    budget = ApiBudget(cfg["budgets"]["max_api_calls"])
    state = load_state()
    log(f"scout cycle start ({day})")

    if "--force-unreachable" in argv or not gh_reachable(budget):
        write_report(day, "unreachable", f"{budget.used()} / {budget.max_calls} calls",
                     {"proposed": [], "examined": [],
                      "errors": ["GitHub unreachable — no scouting performed; "
                                 "state and queue untouched"]},
                     {"candidates": [], "excluded": [], "repos_skipped": [], "errors": []},
                     [])
        git_publish(day, no_git)
        log("mode unreachable: report written, scout_state.json and queue untouched")
        return 1

    mode = "authenticated"
    # Adaptive concurrency (v2.5.0): capacity per repo from repo health;
    # legacy fixed over_concentration_limit remains the fallback when health
    # data is unreadable (discovery must not die from a corrupt state file).
    import repo_health
    health_counts, cap_map = repo_health.scout_counts_or_fallback(cfg)
    counts = health_counts if health_counts is not None else open_pr_counts()
    exclusions = {
        "engaged": set(counts),
        "approved": set(cfg["issue_scout"]["approved_repos"]),
        "denied": set(cfg["repo_scout"].get("denied_repos") or []),
        "profiled": {p[:-5].replace("__", "/")
                     for p in os.listdir(os.path.join(ROOT, "repo_profiles"))
                     if p.endswith(".json")} if os.path.isdir(
                         os.path.join(ROOT, "repo_profiles")) else set(),
    }

    repo_cfg = dict(cfg["repo_scout"])
    repo_cfg["max_new_repos_scored_per_day"] = cfg["budgets"]["max_new_repos_scored_per_day"]
    repo_out = {"proposed": [], "examined": [], "errors": []}
    try:
        repo_out = scout_repos.scout_repos(repo_cfg, state, exclusions, budget, log)
    except BudgetExceeded as e:
        mode = "degraded (budget)"
        repo_out.setdefault("errors", []).append(str(e))

    # Our OWN record with each proposed repo. Reporting only — attached to the
    # proposal dict and rendered below the table, never folded into the score
    # (that would be a Scout v2 semantics change). Runs only for repos that
    # actually reached a proposal, so the cost is ~12 repos/day, cached 14d.
    # Never fatal: a repo we cannot look up is proposed exactly as before.
    try:
        hist_cache = scout_history.load_cache()
        login = scout_history.our_login(budget)
        for p in repo_out.get("proposed", []):
            p["history"] = scout_history.history_with_cache(
                p["repo"], hist_cache, budget, today, login=login)
        scout_history.save_cache(hist_cache)
    except Exception as e:                                  # noqa: BLE001
        repo_out.setdefault("errors", []).append(f"prior-history lookup: {e}")

    issue_cfg = cfg["issue_scout"]
    repo_scores = {r: e.get("score") for r, e in state.get("repos", {}).items()}
    issue_out = {"candidates": [], "excluded": [], "repos_skipped": [], "errors": []}
    if mode == "authenticated":
        seen = seen_issue_keys(state, issue_cfg, today)
        try:
            issue_out = scout_issues.scout_issues(issue_cfg, state, counts,
                                                  repo_scores, seen, budget, log,
                                                  cap_map=cap_map)
        except BudgetExceeded as e:
            mode = "degraded (budget)"
            issue_out.setdefault("errors", []).append(str(e))
    else:
        issue_out["errors"].append("issue scout not run (budget exhausted in repo scout)")

    # probe_errors counts toward degraded too: it is rendered in the report's
    # Errors section, so omitting it here would print errors under a clean
    # "authenticated" header — the report must not look healthier than it is.
    if repo_out.get("errors") or repo_out.get("probe_errors") or issue_out.get("errors"):
        if mode == "authenticated":
            mode = "degraded"

    queued_keys = append_queue(issue_out.get("candidates", []), day)
    save_state(state, day)
    path = write_report(day, mode, f"{budget.used()} / {budget.max_calls} calls",
                        repo_out, issue_out, queued_keys)
    git_publish(day, no_git)

    with open(MARKER, "w") as f:
        f.write(day + "\n")
    flags = []
    if repo_out.get("proposed"):
        flags.append("new-repo-proposal")
    if queued_keys:
        flags.append("queued-candidates")
    log(f"scout cycle complete: mode={mode}, "
        f"{repo_out.get('probes', 0)} readiness probe(s), "
        f"{len(repo_out.get('proposed', []))} repo proposal(s), "
        f"{len(issue_out.get('candidates', []))} candidate(s), "
        f"{len(queued_keys)} queued"
        f"{' — OPERATOR FLAGS: ' + ', '.join(flags) if flags else ''}")
    log(f"report: {os.path.relpath(path, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
