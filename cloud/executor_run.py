"""Daily auto-ship executor — third step of the launchd chain (v2.5.x
controlled experiment). Consumes scout-queued candidates and invokes the
pipeline with auto-ship gates armed; the pipeline itself decides ship vs
queue-for-review at the push decision point (runner/pipeline_v2.py +
agent/autoship_gates.py). This script's job is flow control, not judgment:
freshness, breaker, budgets, capacity, claims, ledger, report.

Hard rules (fail closed, all of them):
- Single instance: an fcntl lock prevents a manual run racing the launchd
  run (double-claim = double-PR risk).
- No monitor success marker for TODAY -> no ships (health data is stale).
- Breaker not armed, un-acknowledged trip condition, blocked_reason set, or
  any auto-shipped ledger PR missing from live monitor state (the breaker
  would be blind to its closure) -> no ships until the operator intervenes.
- Budgets derive from the write-ahead intent ledger, never from memory: an
  intent is appended BEFORE any push-capable invocation, and an unresolved
  intent burns its daily slot forever (a crash mid-push must not free cap).
- Startup reconciliation: every stuck `in_pipeline` claim is resolved
  against pipeline_v2_queue.jsonl (matched by run_id nonce); a shipped
  record found there is synced into tracking; a claim with NO record sets
  blocked_reason (an outward action may have occurred without a trace).
- Outcome classes: {push_failed, pr_create_failed, ship_error_postpush} and
  ANY unknown outcome are treated as outward-or-uncertain -> slot burned,
  blocked_reason, day aborted, never auto-retried.
- Pre-flight refetch of every candidate issue: closed/assigned -> stale;
  injection lint over stored AND fresh text; title+body-hash change ->
  operator re-rank. The FRESH text is what ships.
- Queue mutations are ALWAYS persisted (after hygiene and after the loop).

Flags: --skip-if-done, --no-git, --dry-run (plan only, mutate nothing),
--config <path>, --state-dir <path> (test override for state files; implies
--no-git and redirects tracking-file writes into the state dir).
Exit codes: 0 = complete (incl. disabled/halted), 1 = degraded/aborted,
2 = internal error.

Rollback: executor.enabled=false (chain reverts to monitor+scout exactly).
"""

import collections
import datetime
import fcntl
import hashlib
import json
import os
import subprocess
import sys

CLOUD = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(CLOUD)
sys.path.insert(0, CLOUD)
sys.path.insert(0, ROOT)

import repo_health
import repo_metrics  # noqa: E402
from agent.autoship_gates import injection_lint  # noqa: E402

CONFIG = os.path.join(CLOUD, "scout_config.json")
QUEUE = os.path.join(ROOT, "scout_queue.jsonl")
PIPELINE_QUEUE = os.path.join(ROOT, "pipeline_v2_queue.jsonl")
MONITOR_MARKER = os.path.join(ROOT, "logs", "monitor", "last_success")
REPORTS = os.path.join(CLOUD, "executor")
LOGDIR = os.path.join(ROOT, "logs", "executor")
LOGFILE = os.path.join(LOGDIR, "executor.log")
MARKER = os.path.join(LOGDIR, "last_success")
LOCKFILE = os.path.join(LOGDIR, "executor.lock")

# Pipeline outcomes that provably involved NO outward action. Anything not
# listed here (including future outcome strings) is treated as
# outward-or-uncertain and halts the day (audit C4 fail-closed default).
NO_OUTWARD_OUTCOMES = {"prepared", "queued_for_review", "review", "reject",
                       "no_fix", "skip_duplicate", "blocked_compliance",
                       "insufficient", "blocked", "checkout_failed",
                       "pipeline_error", "no_fix_timeout", "commit_failed",
                       "ship_error_prepush", "no_fix_exhausted",
                       "no_fix_nonconclusion", "changelog_failed"}
# `changelog_failed` returns from _ship_pr_inner BEFORE prepare_commit, so it
# is provably pre-network; it was missing here on first ship and would have
# burned outward budget and aborted the whole day for a local render failure
# (audit BUG2a-int). Deliberately NOT in BLOCKING_OUTCOMES below: the common
# cause is the post-release window where `## UNRELEASED` does not yet exist,
# which is a statement about the calendar, not the issue.
#
# Outcomes whose pipeline record permanently blocks re-processing an issue.
# prepared / queued_for_review / review are NOT terminal (audit M7).
#
# `no_fix_exhausted` is deliberately ABSENT: it means the triage session ran out
# of turns or wall-clock before concluding, which is a statement about our
# budget and not about the issue. It is enumerated in NO_OUTWARD_OUTCOMES above
# because it returns before any ship path, so the C4 fail-closed halt still
# fires for genuinely unknown outcome strings rather than for this one.
BLOCKING_OUTCOMES = {"shipped", "skip_duplicate", "no_fix", "reject",
                     "insufficient", "blocked_compliance"}

# ...but retryable is not unbounded. An issue the triage agent can never
# localise would otherwise consume one of the 3 daily pipeline attempts EVERY
# DAY FOREVER and starve every other candidate. One retry, then it retires.
MAX_EXHAUSTED_ATTEMPTS = 2

# Retryable-but-capped outcomes that are NOT exhaustions. `changelog_failed`
# means the mandatory changelog entry could not be rendered for THIS checkout
# right now — most often because the repo is in the window after a release cut
# where `## UNRELEASED` does not exist and returns with the next merge. That is
# a fact about the calendar, so the issue must come back; but it must not come
# back forever, so it counts toward the same MAX_EXHAUSTED_ATTEMPTS cap as an
# exhaustion. Two failures and it retires (round-2 audit I-F3).
CAPPED_RETRY_OUTCOMES = {"changelog_failed"}


def log(msg):
    line = f"[{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}"
    print(line)
    with open(LOGFILE, "a") as f:
        f.write(line + "\n")


def _safe(repo, number):
    return f"{repo} issue {number}"


def _atomic_json(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def _write_outcomes(path, outcomes):
    """Merge-aware outcomes write (system audit P1-⑤, 2026-09-02).

    The executor loads repo_outcomes.json ONCE and rewrites it whole many
    times across a run that can hold a pipeline subprocess for an hour —
    while preship_check --justify (the operator's mandated pre-ship tool)
    does its own read-modify-write of the same file. A whole-file write
    from the stale snapshot silently erased the operator's concurrent
    entries, undercounting the daily outward cap the gates read from disk.
    Under the shared queue-lock: union in any intents (by issue+ts+run_id)
    and prs (by pr key) present on disk but unknown to our snapshot, and
    never let an in-memory None blocked_reason clear one set on disk."""
    from queue_lock import QueueLock
    with QueueLock(path):
        try:
            disk = json.load(open(path))
        except (OSError, json.JSONDecodeError):
            disk = {}
        have_i = {(i.get("issue"), i.get("ts"), i.get("run_id"))
                  for i in outcomes.get("intents", [])}
        for i in disk.get("intents", []):
            if (i.get("issue"), i.get("ts"), i.get("run_id")) not in have_i:
                outcomes.setdefault("intents", []).append(i)
                have_i.add((i.get("issue"), i.get("ts"), i.get("run_id")))
        have_p = {p.get("pr") for p in outcomes.get("prs", [])}
        for p in disk.get("prs", []):
            if p.get("pr") and p.get("pr") not in have_p:
                outcomes.setdefault("prs", []).append(p)
                have_p.add(p.get("pr"))
        # preship's own record classes — the very rows this fix exists to
        # keep (third-slot since P1-⑤; test-absence added with F2 2026-09-02
        # after an audit proved the union list was not extended with it)
        for key in ("third_slot_justifications", "test_absence_justifications"):
            have_j = {json.dumps(j, sort_keys=True)
                      for j in outcomes.get(key, [])}
            for j in disk.get(key, []):
                if json.dumps(j, sort_keys=True) not in have_j:
                    outcomes.setdefault(key, []).append(j)
        if not outcomes.get("blocked_reason") and disk.get("blocked_reason"):
            outcomes["blocked_reason"] = disk["blocked_reason"]
        _atomic_json(path, outcomes)


def read_queue():
    if not os.path.exists(QUEUE):
        return []
    out = []
    with open(QUEUE) as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def write_queue(entries):
    # Single-writer + merge-on-write (system audit P1-1): the whole-file
    # replace is built from OUR in-memory list, so any row a concurrent
    # scout appended after our read would be silently dropped — and never
    # re-queued, because the scout's seen-keys state already recorded it.
    # Under the queue lock, re-read the file and carry forward rows whose
    # issue_key we have never seen this run.
    from queue_lock import QueueLock
    with QueueLock(QUEUE):
        ours = {e.get("issue_key") for e in entries}
        merged = list(entries)
        if os.path.exists(QUEUE):
            with open(QUEUE) as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("issue_key") and row["issue_key"] not in ours:
                        merged.append(row)
                        ours.add(row["issue_key"])
        tmp = QUEUE + ".tmp"
        with open(tmp, "w") as f:
            for e in merged:
                f.write(json.dumps(e) + "\n")
        os.replace(tmp, QUEUE)


def pipeline_records():
    recs = []
    if os.path.exists(PIPELINE_QUEUE):
        with open(PIPELINE_QUEUE) as f:
            for line in f:
                try:
                    recs.append(json.loads(line))
                except Exception:
                    continue
    return recs


def _exhausted_error(err):
    """Whether an agent_error is a BUDGET exhaustion rather than a conclusion.

    Mirrors runner.pipeline_v2._exhausted, duplicated rather than imported on
    purpose — cloud/ must not take a dependency on runner/, which pulls the
    storage env_guard. Same precedent as autoship_gates._LANG_BY_EXT mirroring
    test_exec.issue_lang; scripts/test_executor.py asserts they cannot diverge.
    Fails closed: anything unrecognised is NOT exhaustion.
    """
    e = (err or "").strip()
    return e == "timeout" or "Reached max turns" in e


# Read-side switch for the non-conclusion classification — LIVE per the
# 2026-08-29 operator ruling (knowledge/ruling_tidb_70636_reclassification_
# 2026-08-28.md, addendum: Option A at the measured 8-record scope). Separate
# from pipeline_v2._NONCONCLUSION_RETRY_ENABLED (write side, dark) because the
# two have different risk profiles: this one changes how EXISTING records are
# read; that one changes what future records say. Rollback: set False —
# classification reverts to pre-ruling behaviour, no data change either way.
_NONCONCLUSION_READTIME_ENABLED = True


def _nonconclusion_error(err):
    """The session did not conclude, for reasons beyond our budget.

    Mirror of runner.pipeline_v2._nonconclusion (same no-import reason as
    `_exhausted_error` above; anti-divergence tested). By agentic_session's
    contract a non-empty err means the session did not exit cleanly, so:
    empty -> clean exit (a judgement); budget signatures -> exhaustion;
    anything else -> a session that never got to conclude.
    """
    e = (err or "").strip()
    return e != "" and not _exhausted_error(err)


def _is_nonconclusion(rec):
    """A `no_fix` record whose own bytes show no judgement was ever formed.

    Same read-time mechanism as `_is_exhaustion` below and the same
    precedent: the ledger is append-only, so records are classified the way
    they would be classified today rather than edited. `stripped_scaffolding`
    keeps the post-strip site terminal — there the agent DID answer.

    These count toward the SAME MAX_EXHAUSTED_ATTEMPTS cap as exhaustions
    (see blocking_seen_keys): retryable is never unbounded, so a key whose
    sessions die twice on environment retires exactly like one that times
    out twice. Measured scope at the 2026-08-29 ruling: 8 records, 6 keys,
    2 of which actually re-enter the pool (etcd#22013, tidb#70636).
    """
    return (_NONCONCLUSION_READTIME_ENABLED
            and rec.get("outcome") == "no_fix"
            and not rec.get("stripped_scaffolding")
            and _nonconclusion_error(rec.get("agent_error")))


def _is_exhaustion(rec):
    """True for a record that means 'we ran out of room', old format or new.

    Records written before 2026-08-08 say `no_fix` but carry the exhaustion
    signature in `agent_error`. They are classified the way they WOULD be
    classified today rather than editing them: pipeline_v2_queue.jsonl is an
    append-only ledger and rewriting past outcomes to suit a new rule destroys
    the evidence that the rule was ever needed.

    `stripped_scaffolding` is the discriminator between the two `no_fix` sites.
    Only the post-strip site sets it, and that site stays terminal by design —
    there the agent DID answer and the answer was entirely scaffolding, which
    is a judgement about its work and not a budget it never got to spend.
    """
    outcome = rec.get("outcome")
    # no_fix_nonconclusion is write-side dark today; recognised here so that
    # if/when pipeline_v2 enables it, those records count toward the same cap.
    if outcome in ("no_fix_exhausted", "no_fix_nonconclusion"):
        return True
    return (outcome == "no_fix"
            and not rec.get("stripped_scaffolding")
            and _exhausted_error(rec.get("agent_error")))


def eligible_candidate(e, seen_blocking, dry_run):
    """Whether a queue entry may be attempted this cycle.

    Module-level (not a closure) so the retry semantics are directly testable —
    the 2026-08-08 gap was found precisely because the queue-status filter and
    the ledger-blocking filter were verified separately and disagreed: the
    ledger said gnolang/gno#5778 was retryable while its queue status
    `no_fix` kept it invisible to the candidate loop forever.
    """
    if e.get("status") == "pending":
        return True
    # Exhausted-but-under-cap candidates re-qualify. Their pipeline record says
    # "ran out of budget", not "no fix exists". Membership in seen_blocking is
    # the ONE authority on the difference: genuine conclusions are in it
    # terminally, capped exhaustions land in it via the attempt counter, and
    # legacy `no_fix` statuses stamped before the outcome split are
    # re-classified by the same read-time rule as their ledger records — the
    # queue status and the ledger can never disagree again.
    if (e.get("status") in ("no_fix", "no_fix_exhausted", "no_fix_nonconclusion",
                            "changelog_failed")
            and e.get("issue_key") not in seen_blocking):
        return True
    # dry-run-evaluated candidates with NO gate failures re-qualify for
    # live mode (audit M7) — the shakedown must not burn the pool
    return (not dry_run and e.get("status") == "dry_run_prepared"
            and not e.get("gate_failures"))


def blocking_seen_keys(records):
    """Issue keys that must not be re-processed.

    Two ways in: a terminal outcome, or MAX_EXHAUSTED_ATTEMPTS non-terminal
    retries (exhaustions, nonconclusions, and CAPPED_RETRY_OUTCOMES all draw
    on the same budget). A single terminal record blocks regardless of how many
    retryable ones sit beside it — terminal is terminal.
    """
    blocked = set()
    exhausted = collections.Counter()
    for e in records:
        key = f"{e.get('repo')}#{e.get('number')}"
        if (_is_exhaustion(e) or _is_nonconclusion(e)
                or e.get("outcome") in CAPPED_RETRY_OUTCOMES):
            exhausted[key] += 1
        elif e.get("outcome") in BLOCKING_OUTCOMES:
            blocked.add(key)
    return blocked | {k for k, n in exhausted.items()
                      if n >= MAX_EXHAUSTED_ATTEMPTS}


def record_for_run(records, repo, number, run_id):
    """Authoritative outcome for OUR invocation, matched by nonce."""
    for e in reversed(records):
        if (e.get("repo") == repo and e.get("number") == number
                and run_id and e.get("run_id") == run_id):
            return e
    return None


def refetch_issue(repo, number):
    p = subprocess.run(["gh", "api", f"repos/{repo}/issues/{number}"],
                       capture_output=True, encoding="utf-8", errors="replace",
                       timeout=60)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "gh failed").strip()[:200])
    return json.loads(p.stdout)


def text_hash(title, body):
    norm = " ".join(((title or "") + "\n" + (body or "")).split())
    return hashlib.sha256(norm.encode()).hexdigest()


def git_publish(day, files, no_git):
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
    # gitignored-and-untracked paths (ops_tracked_prs.json, .gitignore:40) make
    # a fatal `git add` refuse the WHOLE list — and an ignored file cannot
    # dirty a later pull --rebase anyway, which is the only reason state files
    # are committed here (audit H3). Filter them out, loudly, instead of dying
    # at publish after an otherwise-clean cycle (incident: first full-cycle
    # dry-run, 2026-08-02).
    addable = []
    for f in files:
        p = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", f])
        if p.returncode == 0:
            log(f"git: {os.path.relpath(f, ROOT)} is gitignored — not published")
        else:
            addable.append(f)
    run("add", *addable, fatal=True)
    # audit: scope the COMMIT to the same paths as the add (see
    # monitor_run.py:233) — a pathless commit publishes the whole index. An
    # EMPTY pathspec after `--` means "no pathspec" to git, so the empty
    # `addable` case must not reach commit at all.
    c = (run("commit", "-m", f"cloud-executor: daily report {day}", "--", *addable)
         if addable else None)
    if c is not None and c.returncode == 0:
        run("push")
        log("git: report + state committed and pushed")
    else:
        log("git: nothing new to commit")


def write_report(day, lines):
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, f"{day}.md")
    # System audit P2-m (2026-09-02): a halted RE-RUN used to truncate the
    # day's report, replacing the disposition table the operator needs with
    # just the halt banner. A re-run on a day that already has a report
    # APPENDS a clearly-marked section instead of destroying the record.
    if os.path.exists(path):
        with open(path, "a") as f:
            f.write("\n\n---\n## Re-run "
                    f"({datetime.datetime.now().astimezone().isoformat(timespec='seconds')})\n\n"
                    + "\n".join(lines) + "\n")
        return path
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def arg_value(argv, flag):
    return (argv[argv.index(flag) + 1]
            if flag in argv and argv.index(flag) + 1 < len(argv) else None)


def sync_tracking(repo, pr_number, tracked_path, targets_path):
    """Append a shipped PR to both tracking files. Raises on any failure —
    the caller must treat that as breaker blindness (audit invariant).

    Two files cannot be updated atomically without a journal, so this narrows
    the window instead of pretending to close it. BOTH documents are read,
    shape-checked and mutated in memory before EITHER is written, which moves
    every likely failure — file missing, unparseable JSON, unexpected shape —
    in front of the first write. Previously the sequence was read/write/
    read/write, so a corrupt monitor_targets.json was discovered only after
    ops_tracked_prs.json had already been rewritten, leaving the breaker
    watching one list and not the other.

    The residual window is a failed second write (disk full, permissions).
    It is not silently survivable, so the error names exactly which file
    landed: the operator repairing breaker blindness needs to know whether to
    add the PR to the other file or remove it from the first. Appends are
    idempotent so that repair-then-rerun cannot double-count a PR."""
    tracked = json.load(open(tracked_path))
    targets = json.load(open(targets_path))
    if not isinstance(tracked, list):
        raise TypeError(f"{tracked_path}: expected a JSON list, got "
                        f"{type(tracked).__name__}")
    if not isinstance(targets, dict) or not isinstance(targets.get("prs"), list):
        raise TypeError(f"{targets_path}: expected an object with a 'prs' list")

    entry = {"repo": repo, "number": pr_number}
    if entry not in tracked:
        tracked.append(entry)
    if not any(p.get("repo") == repo and p.get("number") == pr_number
               for p in targets["prs"] if isinstance(p, dict)):
        targets["prs"].append(entry)

    _atomic_json(tracked_path, tracked)
    try:
        _atomic_json(targets_path, targets)
    except Exception as e:
        raise RuntimeError(
            f"{repo} PR {pr_number} was written to {os.path.basename(tracked_path)} "
            f"but NOT to {os.path.basename(targets_path)} ({type(e).__name__}: {e}); "
            "the two tracking files are out of sync and must be reconciled by "
            "hand before the breaker can see this PR") from e


def resolve_ship(outcomes, cand, repo, pr_url, day, tracked_path, targets_path):
    """Ledger + tracking updates for a shipped PR. Returns (ok, detail).

    The ledger append is idempotent on the same key the report and the breaker
    read (`"<repo> PR <n>"`), matching sync_tracking. Both halves describe the
    same ship, so they must agree on what a repeat means: a second ledger row
    for one PR double-counts it in repo_health.capacity's `merged_with_us`
    (cloud/repo_health.py) once it merges, silently widening that repo's
    concurrency. Reaching here twice for one PR needs a partial failure to be
    repaired and the day re-run — currently blocked upstream by the
    `in_pipeline` claim check, so this is a latent asymmetry, not a live bug
    (audit, Tier 4 verification round, finding 1)."""
    try:
        pr_number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        key = f"{repo} PR {pr_number}"
        if not any(isinstance(e, dict) and e.get("pr") == key
                   for e in outcomes["prs"]):
            outcomes["prs"].append({"pr": key, "shipped_by": "autoship",
                                    "state": "OPEN", "shipped_day": day})
        outcomes.setdefault("repos", {}).setdefault(repo, {"merged_with_us": 0})
        sync_tracking(repo, pr_number, tracked_path, targets_path)
        return True, f"{repo} PR {pr_number} tracked"
    except Exception as e:
        outcomes["blocked_reason"] = (
            f"tracking sync failed for {repo} PR (from {pr_url}) — breaker is "
            f"blind; no further ships until operator repairs: {e}")
        return False, outcomes["blocked_reason"]


def main(argv):
    os.makedirs(LOGDIR, exist_ok=True)
    day = datetime.date.today().isoformat()
    plan_only = "--dry-run" in argv
    state_dir = arg_value(argv, "--state-dir")
    no_git = "--no-git" in argv or bool(state_dir)  # audit L5
    # System audit P2-b (2026-09-02): --state-dir swaps every safety input
    # (breaker, ledger, monitor state) for sandbox copies but did NOT imply
    # dry-run — a live config could perform REAL outward actions gated by
    # sandbox state. Redirected state now forces plan-only unless the test
    # harness explicitly opts in (it fakes the subprocess, so nothing
    # outward can occur there).
    if state_dir and os.getenv("OMEGA_STATEDIR_ALLOW_OUTWARD") != "1":
        if not plan_only:
            log("--state-dir without OMEGA_STATEDIR_ALLOW_OUTWARD=1 — "
                "forcing --dry-run (audit P2-b: sandbox state must never "
                "gate real outward actions)")
        plan_only = True

    # single instance (audit M5) — manual runs must not race launchd
    lock = open(LOCKFILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another executor instance holds the lock — exiting")
        return 1

    outcomes_path = (os.path.join(state_dir, "repo_outcomes.json")
                     if state_dir else repo_health.OUTCOMES)
    circuit_path = (os.path.join(state_dir, "circuit_state.json")
                    if state_dir else repo_health.CIRCUIT)
    monitor_path = (os.path.join(state_dir, "monitor_state.json")
                    if state_dir else repo_health.MONITOR_STATE)
    tracked_path = (os.path.join(state_dir, "ops_tracked_prs.json")
                    if state_dir else os.path.join(ROOT, "ops_tracked_prs.json"))
    targets_path = (os.path.join(state_dir, "monitor_targets.json")
                    if state_dir else os.path.join(CLOUD, "monitor_targets.json"))

    if "--skip-if-done" in argv and not plan_only:
        done = (os.path.exists(MARKER) and open(MARKER).read().strip() == day
                and os.path.exists(os.path.join(REPORTS, f"{day}.md")))
        if done:
            log(f"executor already ran today ({day}) — skipping (catch-up guard)")
            return 0

    cfg_path = arg_value(argv, "--config") or CONFIG
    cfg_all = json.load(open(cfg_path))
    cfg = cfg_all.get("executor", {})
    approved = set(cfg_all.get("issue_scout", {}).get("approved_repos", []))

    if not cfg.get("enabled", False):
        log("executor disabled (executor.enabled=false) — nothing to do")
        return 0

    dry_run = bool(cfg.get("dry_run", True)) or plan_only
    log(f"executor cycle start ({day}) mode="
        f"{'PLAN' if plan_only else 'DRY-RUN' if dry_run else 'LIVE'}")

    report = [f"# Executor Report — {day}", "",
              f"Mode: {'plan-only' if plan_only else 'dry-run (no outward action)' if dry_run else 'LIVE auto-ship'}",
              "",
              "Every auto-ship must pass all autoship gates; any gate miss "
              "routes the candidate to operator review (statuses persisted in "
              "scout_queue.jsonl; dispositions below are the review record).", ""]

    # 1. freshness: monitor must have succeeded TODAY
    fresh = (os.path.exists(MONITOR_MARKER)
             and open(MONITOR_MARKER).read().strip() == day)
    if not fresh and not state_dir:
        report += ["## ABORT — stale health data",
                   "The monitor has not succeeded today; repo health would be stale. No ships."]
        path = write_report(day, report)
        git_publish(day, [path], no_git or plan_only)
        log("ABORT: monitor success marker not today's — no ships")
        return 1

    monitor_state = json.load(open(monitor_path))
    outcomes = json.load(open(outcomes_path))
    circuit = json.load(open(circuit_path))
    queue = read_queue()
    pipe_records = pipeline_records()

    # 2. startup reconciliation of stuck claims (audit C5): a crashed prior
    # run may have shipped without recording. Resolve every in_pipeline claim
    # against the pipeline queue by nonce before ANY new outward action.
    recon_notes, recon_blocked = [], False
    for cand in queue:
        if cand.get("status") != "in_pipeline":
            continue
        rec = record_for_run(pipe_records, cand.get("repo"), cand.get("number"),
                             cand.get("run_id"))
        key = _safe(cand.get("repo"), cand.get("number"))
        intent = next((i for i in outcomes.get("intents", [])
                       if i.get("issue") == key and i.get("resolved") is None), None)
        if rec is None:
            recon_blocked = True
            outcomes["blocked_reason"] = (outcomes.get("blocked_reason") or
                f"stuck claim {key} has NO pipeline record — an outward action "
                "may have occurred without a trace; operator must verify the "
                "fork/PR state before clearing")
            recon_notes.append(f"{key}: STUCK, no record — blocked_reason set")
            continue
        outcome = rec.get("outcome")
        if outcome == "shipped":
            pr_url = ((rec.get("ship") or {}).get("pr_url")) or ""
            ok, detail = resolve_ship(outcomes, cand, cand["repo"], pr_url,
                                      day, tracked_path, targets_path)
            cand["status"] = "shipped_auto"
            cand["pr_url"] = pr_url
            if intent:
                intent["resolved"] = "shipped"
            recon_notes.append(f"{key}: recovered SHIPPED — {detail}")
            if not ok:
                recon_blocked = True
        else:
            cand["status"] = ("needs_operator" if outcome == "queued_for_review"
                              else outcome)
            if intent:
                intent["resolved"] = (outcome if outcome not in
                                      NO_OUTWARD_OUTCOMES else "no_outward")
            recon_notes.append(f"{key}: recovered outcome {outcome}")
            # System audit P2-a (2026-09-02): a RECOVERED outward-or-uncertain
            # outcome gets the same operator gate the live loop gives it —
            # the old else-branch resolved the intent and then let the day
            # CONTINUE, defeating the never-auto-retried rule exactly when a
            # crash had hidden the failure.
            if outcome not in NO_OUTWARD_OUTCOMES and outcome != "shipped":
                recon_blocked = True
                outcomes["blocked_reason"] = (outcomes.get("blocked_reason") or
                    f"recovered {outcome} for {key} — live branch or PR on "
                    "fork possible; never auto-retried; operator must "
                    "investigate")
                recon_notes.append(f"{key}: outward-class recovery — "
                                   "blocked_reason set")
    if recon_notes:
        report += ["## Startup reconciliation", ""] + [f"- {n}" for n in recon_notes] + [""]
        if not plan_only:
            _write_outcomes(outcomes_path, outcomes)
            write_queue(queue)

    # 3. reconcile ledger with live monitor state. Persist on ANY mutation,
    # not just state transitions: reconcile also records sticky metrics fields
    # (changes_requested, first_activity_day — concurrency policy 7) on days
    # with no transition at all, and those writes must not be lost.
    _pre_reconcile = json.dumps(outcomes, sort_keys=True)
    outcomes, transitions = repo_health.reconcile(outcomes, monitor_state, day)
    if (json.dumps(outcomes, sort_keys=True) != _pre_reconcile
            and not plan_only):
        _write_outcomes(outcomes_path, outcomes)
    if transitions:
        report += ["## Ledger transitions", ""] + [f"- {t}" for t in transitions] + [""]

    # 4. breaker + halt conditions
    def halt(title, detail, extra_files=(), rc=1):
        """Every caller is a state that needs an operator: a tripped or
        disarmed breaker, breaker blindness, a set blocked_reason.

        rc defaults to 1, not 0. Returning 0 made launchd read a halted
        executor as a clean run, and made the SAME condition report differently
        depending on when you looked: the run that sets blocked_reason exits 1
        (it aborts mid-loop), while every run after it re-read that reason,
        halted here, and exited 0. A breaker that has been open for a week is
        not a success — it is the one state that most needs to be visible from
        outside, because nothing ships until someone acts. The kill switch
        (executor.enabled false) still exits 0: that is a deliberate operator
        setting, not a fault.

        Safe under launchd because com.omega.cloud-monitor sets no KeepAlive —
        the job is StartCalendarInterval + RunAtLoad, so a nonzero exit is
        recorded as the last exit status and never restarts the chain. If
        KeepAlive is ever added, it must be `SuccessfulExit: false`-free or
        this turns a halted breaker into a restart loop."""
        report.extend([f"## {title}", detail])
        path = write_report(day, report)
        git_publish(day, [path, *extra_files], no_git or plan_only)
        log(f"{title}: {detail[:160]} (exit {rc})")
        return rc

    if circuit.get("armed") is not True:
        last = (circuit.get("trips") or [{}])[-1]
        return halt("HALTED — circuit breaker open",
                    f"Reason: {last.get('reason', 'unknown')} ({last.get('ts', '?')}). "
                    "Manual re-arm: set armed=true in cloud/circuit_state.json "
                    "with rationale (already-tripped PRs stay acknowledged).")
    acknowledged = {t.get("pr") for t in circuit.get("trips", []) if t.get("pr")}
    trip = repo_health.breaker_should_trip(outcomes, monitor_state, acknowledged)
    if trip:
        reason, pr_key = trip
        if not plan_only:
            circuit["armed"] = False
            circuit.setdefault("trips", []).append(
                {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "reason": reason, "pr": pr_key, "by": "executor_run"})
            _atomic_json(circuit_path, circuit)
        return halt("TRIPPED — circuit breaker",
                    f"Reason: {reason}. Auto-shipping halted; manual re-arm required.",
                    extra_files=(circuit_path,) if not state_dir else ())
    missing = repo_health.missing_autoship_keys(outcomes, monitor_state)
    if missing:
        if not plan_only:
            outcomes["blocked_reason"] = (
                f"auto-shipped PR(s) missing from live monitor state: "
                f"{', '.join(missing)} — the breaker cannot observe their "
                "closure; operator must restore monitoring before clearing")
            _write_outcomes(outcomes_path, outcomes)
        return halt("HALTED — breaker blindness",
                    f"Auto-shipped PR(s) absent from monitor state: {', '.join(missing)}.")
    if outcomes.get("blocked_reason"):
        return halt("HALTED — blocked",
                    f"blocked_reason: {outcomes['blocked_reason']} — operator "
                    "must investigate and clear it in cloud/repo_outcomes.json.")

    # 5. queue hygiene (audit M3: per-entry guards; H2: always persisted)
    hygiene = []
    for e in queue:
        try:
            if e.get("status") == "pending":
                age = (datetime.date.today()
                       - datetime.date.fromisoformat(e.get("day") or day)).days
                if age > cfg.get("pending_expiry_days", 14):
                    e["status"] = "expired"
                    hygiene.append(f"{_safe(e['repo'], e['number'])} — expired ({age}d)")
        except Exception as ex:
            e["status"] = "malformed"
            hygiene.append(f"malformed queue entry ({str(ex)[:80]})")
    if hygiene:
        report += ["## Queue hygiene", ""] + [f"- {h}" for h in hygiene] + [""]
        if not plan_only:
            write_queue(queue)

    # 6. budgets from ledger
    todays = [i for i in outcomes.get("intents", []) if i.get("day") == day]
    # shared definition with the gates' daily_cap — see repo_health (audit H3)
    outward_used = [i for i in todays
                    if repo_health.intent_is_outward(i.get("resolved"))]
    outward_budget = max(0, cfg.get("max_autoship_per_day", 2) - len(outward_used))
    attempts_budget = max(0, cfg.get("max_pipeline_attempts_per_day", 3) - len(todays))
    report += ["## Budgets", "",
               f"- outward: {outward_budget} remaining (cap {cfg.get('max_autoship_per_day', 2)}, "
               f"{len(outward_used)} used incl. unresolved intents)",
               f"- pipeline attempts: {attempts_budget} remaining", ""]

    caps = repo_health.capacity_map(monitor_state, outcomes, cfg, circuit)
    report += ["## Repo capacity (concurrency policy)", "",
               "| repo | open/limit | regime |", "|------|-----------|--------|"]
    open_counts, _ = repo_health._open_prs_by_repo(monitor_state, outcomes)
    for repo, v in caps.items():
        report.append(f"| {repo} | {open_counts.get(repo, 0)}/{v['capacity']} "
                      f"| {'; '.join(v['reasons'])} |")
    report.append("")
    # per-repo maintainer-relationship metrics (concurrency policy 7) —
    # informational; nothing gates on these automatically
    try:
        metrics = repo_metrics.compute(monitor_state, outcomes, cfg, circuit)
        if not state_dir:
            repo_metrics.persist(metrics)
        report += ["## Repo metrics", ""] + repo_metrics.report_rows(metrics)
        report.append("")
    except Exception as e:  # metrics must never abort the cycle
        log(f"repo metrics failed (non-fatal): {e}")

    # 7. candidate loop
    seen_blocking = blocking_seen_keys(pipe_records)
    dispositions = []
    aborted = False

    def eligible(e):
        return eligible_candidate(e, seen_blocking, dry_run)

    # distribution preference (concurrency policy 6): repos where we have
    # fewer open PRs come first, so work spreads across repositories instead
    # of filling one; confidence then age break ties within a repo tier
    pending = sorted([e for e in queue if eligible(e)],
                     key=lambda e: (open_counts.get(e.get("repo"), 0),
                                    -(e.get("confidence") or 0),
                                    e.get("day") or ""))
    for cand in pending:
        if aborted or outward_budget < 1 or attempts_budget < 1:
            break
        try:
            repo, number = cand["repo"], cand["number"]
            key = _safe(repo, number)
        except Exception:
            cand["status"] = "malformed"
            dispositions.append(("(malformed)", "malformed", "missing repo/number"))
            continue

        if repo not in approved:
            cand["status"] = "unapproved_repo"
            dispositions.append((key, "unapproved_repo", "repo not in approved_repos"))
            continue
        # Repos admitted to `approved_repos` under an operator-only restriction
        # (executor.repo_policy[repo].autoship == false) are discoverable but
        # not autonomously shippable: the scout queues candidates and a human
        # takes them through the normal operator flow. Checked BEFORE capacity
        # and before the refetch so no budget is spent; the candidate keeps the
        # `needs_operator` status so it surfaces in the same review channel as
        # every other operator hand-off rather than being silently dropped.
        # autoship_gates has the same check as a fail-closed backstop — this
        # one only saves work, it is not the enforcement point.
        may_autoship, autoship_reason = repo_health.autoship_allowed(repo, cfg)
        if not may_autoship:
            cand["status"] = "needs_operator"
            cand["operator_reason"] = autoship_reason
            dispositions.append((key, "operator_only", autoship_reason))
            continue
        if cand.get("issue_key") in seen_blocking:
            cand["status"] = "already_processed"
            dispositions.append((key, "already_processed",
                                 "pipeline has a terminal record for this issue"))
            continue
        # in-loop ships are visible here because resolve_ship appends the new
        # PR to `outcomes` before returning, and remaining() counts ledger-OPEN
        # PRs the monitor has not fetched yet (audit H4). No separate in-memory
        # counter — two counters is how the two views drifted in the first place.
        rem, reasons = repo_health.remaining(repo, monitor_state, outcomes, cfg,
                                             circuit)
        if rem < 1:
            # stays `pending` so it ships when capacity opens (policy 5); the
            # reason is recorded on the candidate for the saturation ledger
            # (policy 7). Bundles are deliberately NOT pre-generated for
            # saturated repos: the daily pipeline-attempt budget must not burn
            # on unshippable candidates.
            cand["saturation_reason"] = "; ".join(reasons)
            cand["saturation_day"] = day
            dispositions.append((key, "no_capacity", "; ".join(reasons)))
            continue

        try:
            fresh_issue = refetch_issue(repo, number)
        except Exception as e:
            dispositions.append((key, "refetch_failed", str(e)[:120]))
            continue
        if fresh_issue.get("state") != "open":
            cand["status"] = "stale_closed"
            dispositions.append((key, "stale_closed", "issue no longer open"))
            continue
        if fresh_issue.get("assignees"):
            cand["status"] = "stale_assigned"
            dispositions.append((key, "stale_assigned", "issue now assigned"))
            continue
        # per-repo label policy (cli/cli admission 2026-08-31): some repos
        # accept PRs only for issues in a specific label state, so autonomous
        # action requires that state on the LIVE refetched issue. The
        # candidate stays `pending` — maintainers may label it later, and
        # pending expiry cleans it up if they never do. autoship_gates
        # re-verifies live at ship time (the enforcement point); this check
        # only saves the pipeline attempt.
        raw = fresh_issue.get("labels") or []
        names = [x.get("name") if isinstance(x, dict) else x for x in raw]
        labels_ok, labels_why = repo_health.issue_labels_allowed(repo, names, cfg)
        if not labels_ok:
            cand["labels_reason"] = labels_why
            cand["labels_day"] = day
            dispositions.append((key, "policy_labels", labels_why[:200]))
            continue
        fresh_title = fresh_issue.get("title") or ""
        fresh_body = fresh_issue.get("body") or ""
        lint_hit = None
        for label, text in (("stored", f"{cand.get('title', '')}\n{cand.get('body', '')}"),
                            ("fresh", f"{fresh_title}\n{fresh_body}")):
            ok, why = injection_lint(text)
            if not ok:
                lint_hit = f"injection lint ({label}): {why}"
                break
        if lint_hit:
            cand["status"] = "needs_operator"
            cand["operator_reason"] = lint_hit
            dispositions.append((key, "needs_operator", lint_hit))
            continue
        # title changes matter too — the fresh title is what ships (audit L4)
        if text_hash(fresh_title, fresh_body) != text_hash(cand.get("title", ""),
                                                           cand.get("body", "")):
            cand["status"] = "needs_operator"
            cand["operator_reason"] = "issue title/body changed since ranking — re-rank required"
            dispositions.append((key, "needs_operator", cand["operator_reason"]))
            continue

        if plan_only:
            dispositions.append((key, "WOULD RUN",
                                 f"conf {cand.get('confidence')}, remaining capacity {rem}"))
            outward_budget -= 1
            attempts_budget -= 1
            continue

        # claim + write-ahead intent + nonce
        run_id = f"exec-{day}-{number}-{os.getpid()}"
        cand["status"] = "in_pipeline"
        cand["run_id"] = run_id
        cand["claimed_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        write_queue(queue)
        intent = {"day": day, "issue": key, "ts": cand["claimed_at"],
                  "run_id": run_id, "resolved": None}
        outcomes.setdefault("intents", []).append(intent)
        _write_outcomes(outcomes_path, outcomes)
        attempts_budget -= 1

        body_file = os.path.join(LOGDIR, f"issue_{repo.replace('/', '_')}_{number}.txt")
        with open(body_file, "w") as f:
            f.write(fresh_body)
        pipe_log = os.path.join(LOGDIR, f"pipeline_{repo.replace('/', '_')}_{number}_{day}.log")
        args = [sys.executable, "-B", os.path.join(ROOT, "runner", "pipeline_v2.py"),
                "--repo", repo, "--number", str(number),
                "--title", fresh_title, "--body-file", body_file, "--ship"]
        env = dict(os.environ, OMEGA_AUTOSHIP="1", OMEGA_TESTGEN="1",
                   OMEGA_RUN_ID=run_id, OMEGA_EXECUTOR_CFG=cfg_path)
        if state_dir:
            env["OMEGA_STATE_DIR"] = state_dir
        if not dry_run:
            args.append("--push")
            env["ALLOW_PR_CREATION"] = "true"
        log(f"invoking pipeline for {key} (dry_run={dry_run}, run_id={run_id})")
        try:
            p = subprocess.run(args, capture_output=True, encoding="utf-8",
                               errors="replace",
                               timeout=cfg.get("pipeline_timeout_seconds", 3600),
                               env=env, cwd=ROOT, start_new_session=True)
            with open(pipe_log, "w") as f:  # audit L3: keep diagnostics
                f.write((p.stdout or "") + "\n--- stderr ---\n" + (p.stderr or ""))
        except subprocess.TimeoutExpired:
            # System audit P1-① (2026-09-02): timeout is OUTWARD-OR-UNCERTAIN,
            # exactly like the no-record path below — the kill can land after
            # a successful `git push`/`gh pr create` but before the pipeline
            # writes its record, and an in-flight git/gh grandchild can even
            # COMPLETE the outward action after the kill. The old branch
            # overwrote the claim to error_operator (removing it from startup
            # reconciliation's in_pipeline scan) and set no blocked_reason, so
            # autonomy resumed next day with a possibly-live untracked PR.
            # Now: claim STAYS in_pipeline (reconciliation will find either a
            # late-written record or set the no-record block again) and
            # blocked_reason halts autonomy until the operator verifies.
            outcomes["blocked_reason"] = (
                f"pipeline TIMEOUT for {key} — outward-or-uncertain: a killed "
                "push/pr-create may have completed without a trace; claim kept "
                "in_pipeline for reconciliation; operator must verify the "
                "fork/PR state before clearing")
            dispositions.append((key, "timeout_blocked",
                                 outcomes["blocked_reason"]))
            _write_outcomes(outcomes_path, outcomes)
            write_queue(queue)
            aborted = True
            break

        result = record_for_run(pipeline_records(), repo, number, run_id)
        if result is None:
            cand["status"] = "error_operator"
            outcomes["blocked_reason"] = (
                f"invocation for {key} produced no pipeline record — an outward "
                f"action may have occurred without a trace; see {os.path.basename(pipe_log)}")
            dispositions.append((key, "error_operator", outcomes["blocked_reason"]))
            _write_outcomes(outcomes_path, outcomes)
            write_queue(queue)
            aborted = True
            break

        outcome = result.get("outcome")
        if outcome == "shipped":
            intent["resolved"] = "shipped"
            outward_budget -= 1
            pr_url = ((result.get("ship") or {}).get("pr_url")) or ""
            cand["status"] = "shipped_auto"
            cand["pr_url"] = pr_url
            ok, detail = resolve_ship(outcomes, cand, repo, pr_url, day,
                                      tracked_path, targets_path)
            dispositions.append((key, "SHIPPED", pr_url if ok else detail))
            _write_outcomes(outcomes_path, outcomes)
            write_queue(queue)
            try:
                os.remove(body_file)
            except OSError:
                pass
            if not ok:
                aborted = True
                break
        elif outcome not in NO_OUTWARD_OUTCOMES:
            # push_failed / pr_create_failed / ship_error_postpush / anything
            # unknown: outward-or-uncertain — burn, block, abort (audit C4)
            intent["resolved"] = outcome
            outward_budget -= 1
            cand["status"] = "outward_failed_operator"
            outcomes["blocked_reason"] = (
                f"{outcome} for {key} — live branch or PR on fork possible; "
                "never auto-retried; operator must investigate")
            dispositions.append((key, outcome, outcomes["blocked_reason"]))
            _write_outcomes(outcomes_path, outcomes)
            write_queue(queue)
            aborted = True
            break
        elif outcome in ("pipeline_error", "checkout_failed"):
            intent["resolved"] = "no_outward"
            cand["status"] = "error_operator"
            dispositions.append((key, outcome, "environment problem — day aborted"))
            _write_outcomes(outcomes_path, outcomes)
            write_queue(queue)
            aborted = True
            break
        else:
            intent["resolved"] = "no_outward"
            if outcome == "queued_for_review":
                cand["status"] = "needs_operator"
                fails = ((result.get("autoship") or {}).get("failures")) or []
                cand["gate_failures"] = fails
                detail = "; ".join(f"{f['gate']}: {f['reason']}" for f in fails)[:400]
                dispositions.append((key, "queued_for_review",
                                     detail or "gate failures recorded"))
            elif outcome == "prepared":
                cand["status"] = "dry_run_prepared"
                record = result.get("autoship")
                if record is None:
                    detail = "GATES NOT EVALUATED (unexpected — investigate)"
                    cand["gate_failures"] = [{"gate": "missing_record",
                                              "reason": detail}]
                else:
                    fails = record.get("failures") or []
                    cand["gate_failures"] = fails
                    detail = ("all gates PASS (dry-run, no push)" if not fails
                              else "; ".join(f"{f['gate']}: {f['reason']}"
                                             for f in fails)[:400])
                dispositions.append((key, "dry_run_prepared", detail))
            else:
                cand["status"] = {"skip_duplicate": "duplicate",
                                  "no_fix": "no_fix",
                                  "blocked_compliance": "blocked_compliance",
                                  # explicit so the retryable status in
                                  # eligible_candidate is the SAME string the
                                  # ledger records, not an incidental
                                  # fall-through (round-2 audit I-F3)
                                  "changelog_failed": "changelog_failed",
                                  "insufficient": "insufficient",
                                  "reject": "rejected_judge",
                                  "review": "needs_operator"}.get(outcome, outcome)
                dispositions.append((key, outcome,
                                     (result.get("review") or {}).get("reason", "")[:200]))
            _write_outcomes(outcomes_path, outcomes)
            write_queue(queue)
            try:
                os.remove(body_file)
            except OSError:
                pass

    if not plan_only:
        write_queue(queue)  # audit H2: mutations always persisted

    report += ["## Candidate dispositions", ""]
    if dispositions:
        report += ["| candidate | disposition | detail |", "|-----------|-------------|--------|"]
        report += [f"| {k} | {d} | {detail} |" for k, d, detail in dispositions]
    else:
        report.append("No pending candidates eligible today.")
    report.append("")

    path = write_report(day, report)
    ships = sum(1 for _, d, _ in dispositions if d == "SHIPPED")
    publish_files = [path]
    if not plan_only and not state_dir:
        # tracked files the executor may have rewritten MUST be committed or
        # every later pull --rebase fails on the dirty tree (audit H3)
        publish_files += [outcomes_path, tracked_path, targets_path,
                          repo_metrics.METRICS]
    git_publish(day, publish_files, no_git or plan_only)
    if not plan_only and not aborted:
        with open(MARKER, "w") as f:  # audit L8: marker only on clean runs
            f.write(day + "\n")
    log(f"executor cycle complete: {ships} shipped, "
        f"{len(dispositions)} candidate(s) considered, aborted={aborted}"
        f"{' — OPERATOR FLAGS: auto-shipped' if ships else ''}")
    log(f"report: {os.path.relpath(path, ROOT)}")
    return 1 if aborted else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        log(f"executor internal error: {type(e).__name__}: {e}")
        sys.exit(2)
