"""v2 production pipeline — the agentic localize+fix architecture wired end to
end, replacing the invalidated keyword-localization path.

Flow (production, a live issue):
    env_guard.require()            # assert SSD mounted; build cache env
      -> duplicate_check           # skip before spending a session (PR/fix exists?)
      -> prepare_repo_head()       # clone/refresh repo at default-branch HEAD on SSD
      -> repo intelligence         # contribution requirements / compliance profile
      -> agentic_fix()             # constrained claude -p session edits the tree
      -> capture diff              # the patch, working tree left patched
      -> run_package_tests()       # affected tests build & pass? (no ground truth)
      -> review_patch() (judge 1)  # single-patch reviewer -> ship/review/reject
      -> regression-test gen       # must FAIL on base / PASS on patch; fail-closed
      -> judge 2 + 16 ship gates   # autoship path only: second judge pass reads
                                   # neutralized issue text; every gate must pass
      -> ship() or queue           # create_pr() ready-for-review, or human queue

Two design facts kept honest:
  * A live issue has no merged human fix, so the *calibrated* comparison judge
    and the full Stage-3 differential cannot run inline — those need ground
    truth and live in the offline eval (run_eval / benchmark suite).
  * Nothing ships un-gated. ``create_pr`` (gated by ALLOW_PR_CREATION) opens
    a ready-for-review PR; by default the pipeline is dry-run and only emits a
    PR-ready bundle.

Usage:
    # offline integration check on one ground-truth issue (drives the shared
    # agentic_session + judge + regression_harness modules together):
    python3 runner/pipeline_v2.py --eval ethereum/go-ethereum#35018

    # production dry-run on a live issue (build & verify a local commit):
    python3 runner/pipeline_v2.py --repo owner/repo --number 123 \
        --title "..." --body-file issue.txt --ship
    # add --push to actually push the branch + open the PR (outward).
"""

import json
import os
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from runner import env_guard  # noqa: E402
from runner.env_guard import REPOS_WORKSPACE  # noqa: E402
from agent.agentic_session import agentic_fix, capture_diff  # noqa: E402
from agent import judge as judge_mod  # noqa: E402
from agent import regression_harness as rh  # noqa: E402
from agent import contribution_reqs  # noqa: E402
from agent import repo_profile  # noqa: E402
from agent import duplicate_check  # noqa: E402
from agent import test_generation  # noqa: E402
from agent.test_exec import test_files_of  # noqa: E402

QUEUE_FILE = os.path.join(_ROOT, "pipeline_v2_queue.jsonl")
# Stamped onto every bundle for the reject taxonomy / per-reject version
# tracking. Pure metadata — does NOT affect any pipeline decision. Bump on
# each versioned release.
#
# v2.5.1: the four-tier audit of the v2.5.0 auto-ship experiment. The gates
# changed behaviour (injection lint rebuilt around a gate/redactor split,
# judge 2 reads neutralized text, judge INFRA failures no longer recorded as
# disagreements, test-dir classification by filename), so bundles produced
# before and after are not comparable on gate evidence — which is exactly what
# this stamp exists to disambiguate in the reject taxonomy.
PIPELINE_VERSION = "v2.5.1"

# Whether our submitting account is human-owned (repos like terraform forbid
# bot-account submissions). Default true — the account is the operator's.
HUMAN_OWNED = os.getenv("OMEGA_HUMAN_OWNED", "true").lower() == "true"

# v2.5.0 controlled auto-ship experiment: when set (ONLY by
# cloud/executor_run.py), an outward push additionally requires a second
# independent judge pass and every gate in agent/autoship_gates.py to pass;
# any gate miss downgrades the push to a local prepared bundle
# (outcome "queued_for_review"). Without this env the push decision is
# byte-for-byte the v2.4.0 behavior.
AUTOSHIP = os.getenv("OMEGA_AUTOSHIP", "0") == "1"

# Production triage budget — abandon low-confidence investigations before the
# agent's full 1200s capacity. Env-tunable. Identified by the readiness sweep
# (no_fix cases burned up to 1211s).
#
# Raised 2026-08-08 (50→75 turns, 600→900s) on a measurement, not a hunch:
# gnolang/gno 5778 spent 487.8s over 50 turns = 9.76s/turn, so the 600s clock
# would have bound at ~61 turns anyway — the two budgets sat within 20% of each
# other and raising turns alone would only have moved the death from the turn
# counter to the clock. They have to move together, and TURNS must stay the
# binding constraint: "abandon low-confidence investigations" is a turn-shaped
# idea, and a time-bound triage is silently machine-speed-dependent.
# 75 turns x 9.76s ~= 732s < 900s, so turns still bind first.
# CAVEAT: 9.76s/turn is ONE observation on ONE 2.5GB checkout. It is enough to
# show the budgets were mismatched (that holds across a wide band of per-turn
# cost); it is NOT enough to claim 75 is the right number. Revisit once
# `no_fix_exhausted` has made exhaustion countable over several events.
# Rollback needs no code change: OMEGA_TRIAGE_MAX_TURNS=50 OMEGA_TRIAGE_TIMEOUT=600
TRIAGE_TIMEOUT = int(os.getenv("OMEGA_TRIAGE_TIMEOUT", "900"))
TRIAGE_MAX_TURNS = int(os.getenv("OMEGA_TRIAGE_MAX_TURNS", "75"))


def _exhausted(err):
    """True when the triage session died on its BUDGET rather than concluding.

    `no_fix` conflated two different claims: "the agent found no fix" and "the
    agent ran out of room before answering". Only the first justifies retiring
    an issue forever. Measured over the whole 60-record queue corpus, ALL FOUR
    `no_fix` records were exhaustion (3 timeout, 1 max-turns) — the outcome has
    never once meant what its name says.

    `agent/agentic_session.py` is FROZEN_SRC and is not touched; it already
    emits exactly two stable signatures, matched here:
        wall-clock -> "timeout"          (agentic_fix's TimeoutExpired branch)
        turns      -> "exit_1: Error: Reached max turns (N)"

    FAILS CLOSED: anything unrecognised — including "" — returns False and so
    stays terminal `no_fix`. If the agent's error text ever changes, this
    degrades to today's behaviour, never to something looser.
    """
    e = (err or "").strip()
    return e == "timeout" or "Reached max turns" in e


# Write-side switch for the non-conclusion outcome split
# (knowledge/design_nonconclusion_vs_judgement_2026-08-28.md §7). Landed DARK
# 2026-08-29; ENABLED 2026-08-31 by operator go after the design's required
# observation window: two clean live cycles of the read-side rule
# (etcd#22013 and tidb#70636 re-entered and got genuine verdicts;
# geth#35275 retired correctly at the cap). From here, a session that dies
# without concluding writes `no_fix_nonconclusion` — already recognised by
# NO_OUTWARD_OUTCOMES, _is_exhaustion's cap counting, and
# eligible_candidate. Rollback: set False — strictly the prior behaviour;
# rows already written stay readable (C4 halts on unknowns, by design).
# The READ-side historical classification lives in cloud/executor_run.py
# behind its own switch; the two remain independently revertible.
_NONCONCLUSION_RETRY_ENABLED = True


def _nonconclusion(err):
    """True when the session did not conclude, for reasons beyond our budget.

    By agent/agentic_session.py's contract (FROZEN_SRC, lines 129-148),
    `err` is non-empty if and only if the session did not exit cleanly. So
    "did the agent conclude?" is a recorded boolean, not a signature list:
    an empty err is a clean exit (a judgement), the two budget signatures
    are `_exhausted`, and EVERYTHING ELSE — auth failures, session limits,
    storage aborts, signatures that do not exist yet — is a session that
    never got to conclude and must not retire an issue forever.

    Mirrors cloud.executor_run._nonconclusion_error, duplicated rather than
    imported for the same reason as `_exhausted` above; the anti-divergence
    test in scripts/test_executor.py covers both pairs.
    """
    e = (err or "").strip()
    return e != "" and not _exhausted(err)


def _empty_diff_outcome(err):
    """Outcome for a session that produced no patch. Pure, so both switch
    positions are directly testable — the dark branch would otherwise be
    unreachable by any test until the day it is enabled in production."""
    if _NONCONCLUSION_RETRY_ENABLED and _nonconclusion(err):
        return "no_fix_nonconclusion"
    return "no_fix_exhausted" if _exhausted(err) else "no_fix"


# Agent-created reproduction scaffolding: matched on whole path SEGMENTS (dirs
# or filename stem), never as raw substrings — broad tokens like "sandbox" or
# "poc" occur in legitimate product paths. Only NEW, non-test files are stripped
# (see _strip_scaffolding), so real source is never touched. Readiness sweep saw
# e.g. a `tmprepro/` terraform project leak into a patch.
_SCAFFOLD_SEGMENTS = frozenset((
    "repro", "reproduction", "reproducer", "tmprepro",
    "scratch", "scratchpad", "throwaway"))

# Amendment 2026-08-26 (knowledge/amendment_design_scaffold_strip_2026-08-25.md):
# exact-match segments miss novel agent scratch names (.tmp-incl-test leaked
# into go-task#2878's bundle). Additional rule: a segment is also scaffolding
# if, after stripping leading dots, it starts with "tmp" — EXCEPT the exact
# segment "tmpl" (template convention; the design's own V3 negative control,
# carved out because "tmpdir"-vs-"tmpl" is not separable by any char rule).
# Gated OFF by default — deploy after the pilot cohort closes (design §6) by
# setting OMEGA_SCAFFOLD_TMP_PREFIX=1; unset/0 is byte-identical to the
# pre-amendment predicate. Rollback: unset the var (design §8).
_SCAFFOLD_TMP_PREFIX = os.getenv("OMEGA_SCAFFOLD_TMP_PREFIX", "0") == "1"


def _tmp_prefixed(seg):
    s = seg.lstrip(".")
    return s.startswith("tmp") and s != "tmpl"


def _git(args, cwd=None, env=None, timeout=600):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          env=env)


def _is_scaffolding(f):
    """True if any path segment (dir or filename stem) is a scaffold marker."""
    segs = f.lower().replace("\\", "/").split("/")
    segs = segs[:-1] + [os.path.splitext(segs[-1])[0]]  # last -> stem
    if any(s in _SCAFFOLD_SEGMENTS for s in segs):
        return True
    return _SCAFFOLD_TMP_PREFIX and any(_tmp_prefixed(s) for s in segs)


def _strip_scaffolding(path, changed_files, env):
    """Remove agent-created reproduction scaffolding from the working tree so it
    never reaches review/commit. Only NEW files (absent in HEAD) are removed, so
    real source the agent modified is never touched. Returns stripped paths."""
    stripped = []
    for f in changed_files:
        if not _is_scaffolding(f):
            continue
        if test_files_of([f]):
            continue  # a test file — let the test-only classifier handle it
        in_head = _git(["cat-file", "-e", f"HEAD:{f}"], cwd=path,
                       env=env).returncode == 0
        if in_head:
            continue  # a real tracked file — leave it
        full = os.path.join(path, f)
        if os.path.isfile(full):
            os.remove(full)
            stripped.append(f)
    return stripped


def _gh_username():
    """The authenticated GitHub login (for the fork's PR head). Self-contained
    so production carries no legacy contribution_loop dependency."""
    r = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                       capture_output=True, encoding="utf-8", errors="replace",
                       timeout=20)
    return r.stdout.strip() if r.returncode == 0 else ""


def _purge_appledouble(path, env=None):
    """Remove exFAT AppleDouble ._* companions. `git reset --hard` rewrites
    files on the SSD's exFAT volume, which regenerates these — left behind they
    keep `git status` dirty and pollute the next session's Glob results."""
    subprocess.run(["find", path, "-name", "._*", "-delete"], env=env,
                   capture_output=True)


def _reset_clean(path, env):
    """Restore the checkout to a pristine HEAD: discard the patch, drop
    untracked files, and purge AppleDouble junk."""
    _git(["reset", "--hard", "-q"], cwd=path, env=env)
    _git(["clean", "-fdq"], cwd=path, env=env)
    _purge_appledouble(path, env)


def prepare_repo_head(repo, env):
    """Clone (cached) or refresh ``repo`` at its default-branch HEAD, on the
    SSD. Returns the checkout path or None. Mirrors prepare_checkout's hygiene
    (clean tree + AppleDouble purge) but tracks the live default branch instead
    of a fix commit's parent."""
    safe = repo.replace("/", "__")
    path = os.path.join(REPOS_WORKSPACE, safe)
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(REPOS_WORKSPACE, exist_ok=True)
        r = _git(["clone", "--depth", "1", "--quiet",
                  f"https://github.com/{repo}.git", path], env=env)
        if r.returncode != 0:
            print(f"  clone failed: {r.stderr.strip()[:160]}")
            return None
    else:
        fr = _git(["fetch", "--depth", "1", "--quiet", "origin"], cwd=path, env=env)
        if fr.returncode != 0:
            # Don't run the agent on a stale tree on a transient network failure.
            print(f"  fetch failed: {fr.stderr.strip()[:160]}")
            return None
        # Reset hard to the remote default branch (origin/HEAD -> origin/main…)
        ref = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
                   cwd=path, env=env).stdout.strip()
        target = ref.replace("refs/remotes/", "") if ref else "FETCH_HEAD"
        rr = _git(["reset", "--hard", "--quiet", target], cwd=path, env=env)
        if rr.returncode != 0:
            print(f"  reset failed: {rr.stderr.strip()[:160]}")
            return None
    _git(["clean", "-fdq"], cwd=path, env=env)
    subprocess.run(["find", path, "-name", "._*", "-delete"], env=env,
                   capture_output=True)
    return path


# ── production ─────────────────────────────────────────────────────────────

# Held for process lifetime (one issue per process); flock releases on exit.
_CHECKOUT_LOCK_FH = []


def _chain_guard_and_checkout_lock(repo):
    """System audit P1-⑥ (operator-directed fix 2026-09-02). Returns an
    error string (-> checkout_failed, already in NO_OUTWARD_OUTCOMES) or
    None to proceed.

    (a) Chain guard: a MANUAL pipeline run while the daily chain is
    mid-flight was the last unguarded session-vs-chain entry point; the
    chain's own executor-spawned pipelines inherit OMEGA_CHAIN_PID and
    pass. Dead marker pids are ignored (stale-safe).
    (b) Per-repo checkout lock: prepare_repo_head reuses one deterministic
    working tree per repo and capture_diff records whatever is in that
    tree — two concurrent runs on the same repo could ship a contaminated
    diff. Non-blocking flock; the loser refuses instead of corrupting.
    """
    import fcntl
    cpf = os.path.join(_ROOT, "logs", "chain_running.pid")
    if os.path.exists(cpf):
        try:
            cpid = int(open(cpf).read().strip() or "0")
            alive = cpid > 0 and (os.kill(cpid, 0) or True)
        except (ValueError, OSError):
            alive = False
        if alive and os.getenv("OMEGA_CHAIN_PID") != str(cpid):
            return (f"daily chain is running (pid {cpid}) — manual pipeline "
                    "run refused (audit P1-⑥); retry after the chain")
    os.makedirs(REPOS_WORKSPACE, exist_ok=True)
    lockp = os.path.join(REPOS_WORKSPACE,
                         repo.replace("/", "__") + ".pipelock")
    fh = open(lockp, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return (f"checkout for {repo} is locked by a concurrent pipeline "
                "run — refusing to share the working tree (audit P1-⑥)")
    fh.write(str(os.getpid()))
    fh.flush()
    _CHECKOUT_LOCK_FH.append(fh)
    return None


def run_production(issue, *, ship=False, push=False, check_duplicates=True,
                   env=None, verbose=True):
    """Run the agentic pipeline on a live issue dict {repo, number, title,
    body}. Returns a result bundle.

    Pipeline: duplicate check -> repository intelligence -> agentic fix ->
    tests -> review -> compliance gate -> ship. ``check_duplicates=False``
    disables the pre-work duplicate skip (e.g. for offline evaluation).

    ship=True  -> go through the ship path (build a real local commit).
    push=True  -> additionally push + open the PR (outward, human-approved;
                  implies ship). Default: dry-run -> human-review queue."""
    ship = ship or push
    env = env or env_guard.require(verbose=verbose)
    repo = issue["repo"]
    number = issue.get("number")
    t0 = time.time()
    bundle = {"repo": repo, "number": number, "title": issue.get("title", ""),
              "pipeline_version": PIPELINE_VERSION,
              # Read by the reject taxonomy (ts precedence 1); date-accurate
              # since a run never spans days. Pure metadata, like the version.
              "outcome_ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              # Caller-supplied nonce so the executor matches its own
              # invocation's queue record exactly instead of comparing
              # timestamp strings (audit M4). Empty for manual runs.
              "run_id": os.getenv("OMEGA_RUN_ID", "")}

    # ── Duplicate detection (before we spend a session) ──
    if check_duplicates and number:
        dup = duplicate_check.find_duplicates(repo, number)
        bundle["duplicate"] = dup
        if dup["is_duplicate"]:
            bundle["outcome"] = "skip_duplicate"
            bundle["seconds"] = round(time.time() - t0, 1)
            with open(QUEUE_FILE, "a") as f:
                f.write(json.dumps(bundle, default=str) + "\n")
            return bundle

    guard_fail = _chain_guard_and_checkout_lock(repo)
    if guard_fail:
        bundle["outcome"] = "checkout_failed"
        bundle["detail"] = guard_fail
        bundle["seconds"] = round(time.time() - t0, 1)
        with open(QUEUE_FILE, "a") as f:
            f.write(json.dumps(bundle, default=str) + "\n")
        return bundle
    path = prepare_repo_head(repo, env)
    if not path:
        bundle["outcome"] = "checkout_failed"
        bundle["seconds"] = round(time.time() - t0, 1)
        with open(QUEUE_FILE, "a") as f:
            f.write(json.dumps(bundle, default=str) + "\n")
        return bundle

    # ── Repository Intelligence — persistent profile of ALL contribution
    # requirements/policies (re-scanned only when repo HEAD changes). ──
    head_sha = _git(["rev-parse", "HEAD"], cwd=path, env=env).stdout.strip()
    profile = repo_profile.get_profile(path, repo, head_sha=head_sha)
    reqs = contribution_reqs.detect(path)
    ai_req = profile["requirements"]["ai_disclosure"]
    ai_marker = ai_req.get("marker")
    # Disclosure goes in the PR body when the policy requires it but the repo
    # defines no title marker (e.g. caddy's "Assistance Disclosure" section).
    ai_body = bool(ai_req.get("required")) and not ai_marker
    bundle["contrib_reqs"] = {
        "changelog": (reqs["changelog"] or {}).get("kind"),
        "dco": reqs["dco"], "cla": reqs["cla"], "notes": reqs["notes"]}
    bundle["repo_profile"] = {k: v for k, v in profile["requirements"].items()}

    # Agentic fix — keep the working tree patched (reset=False) for shipping.
    # Production runs under a triage budget so it abandons low-confidence
    # investigations well before the agent's full capacity.
    diff, declared, err = agentic_fix(
        path, issue.get("issue_key", f"{repo}#{issue.get('number')}"),
        issue.get("title", ""), issue.get("body", ""),
        max_turns=TRIAGE_MAX_TURNS, timeout=TRIAGE_TIMEOUT, reset=False, env=env)
    bundle["declared"] = declared
    bundle["agent_error"] = err
    if not diff.strip():
        # An empty diff means one of two things and they are NOT the same:
        # the agent concluded there is nothing to fix, or it never got to
        # conclude. Only the former should retire the issue forever.
        bundle["outcome"] = _empty_diff_outcome(err)
        bundle["diff"] = diff
        bundle["seconds"] = round(time.time() - t0, 1)
        with open(QUEUE_FILE, "a") as f:
            f.write(json.dumps(bundle, default=str) + "\n")
        _reset_clean(path, env)
        return bundle

    # Everything after this point runs with the tree PATCHED (reset=False). Wrap
    # it so ANY exception (strip, classify, tests, review, compliance gate,
    # ship) cleans the tree and records `pipeline_error` — a raise can never
    # leave the checkout dirty, vanish from the queue, or skip the compliance
    # gate on the way to an outward action. (Audit: the strip/classify stages
    # used to sit outside the wrapper.)
    try:
        # Strip agent-created reproduction scaffolding before anything sees the
        # patch; re-capture the cleaned diff from the working tree.
        stripped = _strip_scaffolding(path, rh._changed_files(diff), env)
        if stripped:
            diff = capture_diff(path, reset=False, env=env)
            bundle["stripped_scaffolding"] = stripped
        bundle["diff"] = diff
        if not diff.strip():
            # Deliberately stays terminal `no_fix` even when the session was
            # exhausted. Reaching here means the agent DID produce a patch and
            # every file in it was reproduction scaffolding — a judgement about
            # the work it did, not a budget it never got to spend. Retrying an
            # agent that answers with scaffolding just buys more scaffolding.
            bundle["outcome"] = "no_fix"
            bundle["seconds"] = round(time.time() - t0, 1)
            with open(QUEUE_FILE, "a") as f:
                f.write(json.dumps(bundle, default=str) + "\n")
            _reset_clean(path, env)
            return bundle

        changed = rh._changed_files(diff)
        lang = rh.issue_lang(changed or [""])

        # A patch that only touches test files is not a fix — classify as
        # insufficient (the agent wrote a repro test instead of fixing the bug).
        if changed and set(changed) == set(test_files_of(changed)):
            bundle["review"] = {"verdict": "insufficient",
                                "reason": "patch only modifies test files (no fix)"}
            bundle["action"] = "reject"
            bundle["lang"] = lang
            bundle["changed_files"] = changed
            bundle["outcome"] = "insufficient"
            bundle["seconds"] = round(time.time() - t0, 1)
            with open(QUEUE_FILE, "a") as f:
                f.write(json.dumps(bundle, default=str) + "\n")
            _reset_clean(path, env)
            return bundle
        # Affected-package tests on the patched tree (no ground truth available).
        passed, status, out = rh.run_package_tests(path, changed, lang,
                                                   human_diff="")
        if passed is True:
            test_summary = "affected package tests pass"
        elif passed is False:
            test_summary = "affected package tests fail"
        elif status == "timeout":
            test_summary = "affected tests timed out (inconclusive)"
        elif status == "toolchain_skipped":
            test_summary = f"no toolchain for {lang} (tests not run)"
        else:
            test_summary = "no runnable test for changed files"
        bundle["test"] = {"passed": passed, "status": status, "summary": test_summary}

        # Single-patch reviewer (NOT the calibrated comparison judge).
        verdict = judge_mod.review_patch(issue.get("title", ""),
                                         issue.get("body", ""), diff,
                                         test_summary=test_summary)
        action = judge_mod.gate(verdict.get("verdict", ""))
        # A patch that fails the affected tests can never be a ship candidate.
        if passed is False:
            action = "reject"
        bundle["review"] = verdict
        bundle["action"] = action
        bundle["lang"] = lang
        bundle["changed_files"] = changed
        bundle["seconds"] = round(time.time() - t0, 1)

        # ── Regression-test generation (v2.4) — for a ship-worthy fix, try to
        # generate the fix's own targeted test and verify it against the
        # acceptance invariant (base FAILS, patched PASSES) + an independent
        # fix-blind validity judge. On success the tree becomes fix+test and
        # the bundle diff carries the verified test (the exact shipped
        # artifact); on any fail-closed outcome the tree is restored to
        # fix-only and the candidate simply lacks a generated test (which the
        # auto-ship test-standard gate treats as route-to-review). Never blocks
        # the fix; never weakens a gate. ──
        # ENABLED in v2.4.0 (2026-07-28) after validation: mechanical audit +
        # judge-quality (anti-superficial) + 4/4 blind historical replay all
        # passed (see knowledge/v24_replay.md). Default ON; set OMEGA_TESTGEN=0
        # to disable. Fail-closed on every miss — never blocks the fix.
        if action != "reject" and os.getenv("OMEGA_TESTGEN", "1") == "1":
            base_sha = _git(["rev-parse", "HEAD"], cwd=path,
                            env=env).stdout.strip()
            tg = test_generation.generate_regression_test(
                path, issue, base_sha, diff, changed, lang, env)
            bundle["test_generation"] = {k: v for k, v in tg.items()
                                         if k != "test_diff"}
            if tg.get("outcome") == "generated":
                changed = changed + [tg["test_file"]]
                diff = capture_diff(path, reset=False, env=env)
                bundle["changed_files"] = changed
                bundle["diff"] = diff
            bundle["seconds"] = round(time.time() - t0, 1)

        # ── Compliance Gate (Repository Intelligence) — before any outward
        # action. If a MANDATORY requirement can't be satisfied, STOP. ──
        if action != "reject" and ship:
            # The gate must reflect a REAL duplicate result, never an assumption.
            # Re-check when the early skip was disabled (check_duplicates=False)
            # AND before any outward push — the pre-session result is 10-20 min
            # stale by now and a competing PR may have appeared meanwhile.
            is_dup = (bundle.get("duplicate") or {}).get("is_duplicate")
            if number and (is_dup is None or push):
                dup = duplicate_check.find_duplicates(repo, number)
                bundle["duplicate"] = dup
                is_dup = dup["is_duplicate"]
            # Dry-run the changelog render against THIS patch so the gate and
            # the ship path answer the same question. Without it the gate asks
            # only "does a renderer exist for this repo" and can pass a bundle
            # ship_pr then rejects with `changelog_failed` (audit BUG5-int).
            cl_ok, cl_why, cl_transient = contribution_reqs.changelog_renderable(
                path, reqs, issue, (declared or {}).get("summary"),
                repo=repo, changed_files=changed)
            if not cl_ok and cl_transient:
                # NOT a compliance blocker. `blocked_compliance` is terminal on
                # the first record, and the usual cause here is the release
                # window in which `## UNRELEASED` does not exist — a fact about
                # the calendar, not this issue. Retiring a good candidate for
                # that would be worse than the bug this probe fixes (round-2
                # audit F1). `changelog_failed` is retryable-but-capped.
                bundle["outcome"] = "changelog_failed"
                bundle["changelog_error"] = cl_why
                bundle["seconds"] = round(time.time() - t0, 1)
                _reset_clean(path, env)
                with open(QUEUE_FILE, "a") as f:
                    f.write(json.dumps(bundle, default=str) + "\n")
                return bundle
            report = repo_profile.compliance_report(
                profile, is_duplicate=bool(is_dup), human_owned=HUMAN_OWNED,
                changelog_probe=(cl_ok, cl_why))
            bundle["compliance"] = {
                "ok": report["ok"], "rows": report["rows"],
                "blockers": [b["requirement"] for b in report["blockers"]]}
            if not report["ok"]:
                bundle["outcome"] = "blocked_compliance"
                bundle["seconds"] = round(time.time() - t0, 1)
                _reset_clean(path, env)
                with open(QUEUE_FILE, "a") as f:
                    f.write(json.dumps(bundle, default=str) + "\n")
                return bundle

        # A judge INFRA failure (timeout/unparsed after retry) means the patch
        # is UNJUDGED: it stays in the human-review queue but must never take
        # the ship path — "review" is the ship-candidate action, and with
        # --push an unjudged patch could otherwise go outward (audit v2.3.5).
        unjudged = verdict.get("verdict") in ("judge_timeout", "judge_unparsed")
        if action != "reject" and ship and not unjudged:
            do_push = push
            if AUTOSHIP:
                # ALWAYS evaluate when autoship mode is on — dry-run shakedowns
                # must produce REAL gate records, not fabricated passes (audit
                # C3); the result only downgrades the push when pushing.
                ok, record = _autoship_check(bundle, issue)
                bundle["autoship"] = record
                if push and not ok:
                    do_push = False  # fail closed: build locally, no outward action
            res = ship_pr(repo, issue, path, changed, declared, env,
                          do_push=do_push, reqs=reqs, ai_marker=ai_marker,
                          ai_body=ai_body)
            bundle["ship"] = res
            # Only relabel an ACTUALLY-prepared bundle. `ship_pr` can return
            # before `prepare_commit` ever runs (changelog_failed, ship_error),
            # and calling that "queued_for_review" hands the operator a review
            # item with no branch and no commit behind it, while also hiding a
            # retryable outcome behind a terminal-looking one (round-2 audit
            # I-F2).
            if (AUTOSHIP and push and not do_push
                    and res.get("status") == "prepared"):
                bundle["outcome"] = "queued_for_review"
            else:
                bundle["outcome"] = res.get("status", "ship_error")
            # ship_pr restores the tree itself.
        else:
            bundle["outcome"] = action
            _reset_clean(path, env)
    except Exception as e:
        bundle["outcome"] = "pipeline_error"
        bundle["error"] = f"{type(e).__name__}: {str(e)[:180]}"
        bundle["seconds"] = round(time.time() - t0, 1)
        try:
            _reset_clean(path, env)
        except Exception:
            pass
        with open(QUEUE_FILE, "a") as f:
            f.write(json.dumps(bundle, default=str) + "\n")
        return bundle

    # Persist EVERY terminal bundle, shipped included — ops_report derives the
    # outcome distribution and PR auto-registration (ship.pr_url) from the
    # queue, so a missing "shipped" line left the issue's stale prepared/review
    # record as its final state forever (audit finding).
    bundle["seconds"] = round(time.time() - t0, 1)
    with open(QUEUE_FILE, "a") as f:
        f.write(json.dumps(bundle, default=str) + "\n")
    return bundle


_JUDGE_GATES = ("judge_correct_twice", "no_material_caveats")


def _autoship_check(bundle, issue):
    """v2.5.0 auto-ship gate check at the push decision point.

    Two phases. The PRE-PASS evaluates every gate with no second verdict; if
    anything other than the two judge-2-dependent gates already fails, the
    candidate cannot ship no matter what a second judge would say, so the pass
    is not spent and the record says exactly that (audit M4 — a shakedown day
    of dry-run candidates used to buy a second opinion on patches already
    rejected by testgen). Otherwise the second pass runs on NEUTRALIZED issue
    text (audit M1) and the gates are evaluated for real.

    Returns (ok, record). Fails closed on ANY internal error — a broken check
    can only block a push, never enable one."""
    try:
        from agent import autoship_gates
        # Thread the executor's config + state-dir so gates and executor
        # never diverge (audit M2); exclude this candidate's own in-flight
        # write-ahead intent from the daily cap (audit H1).
        cfg_path = os.getenv("OMEGA_EXECUTOR_CFG") or os.path.join(
            _ROOT, "cloud", "scout_config.json")
        # NO inner exception swallowing (system audit P1-②, operator-directed
        # fix 2026-09-02): `except: cfg = {}` made repo_autoship_allowed and
        # repo_required_labels pass VACUOUSLY when the config could not be
        # read — fail-open at the declared enforcement point. A read failure
        # now propagates to this function's outer handler, which fails the
        # whole autoship check closed (queued_for_review), matching the
        # module contract that a broken input can only block a ship.
        cfg = json.load(open(cfg_path)).get("executor", {})
        title, body = issue.get("title", ""), issue.get("body", "")
        common = dict(issue_title=title, issue_body=body, root=_ROOT,
                      config=cfg, state_dir=os.getenv("OMEGA_STATE_DIR") or None,
                      current_issue=f"{issue.get('repo')} issue {issue.get('number')}")

        skip = "deterministic gate(s) already failed: "
        pre = autoship_gates.evaluate(bundle, second_verdict=None,
                                      second_skipped_reason=skip + "(pre-pass)",
                                      **common)
        blocking = [f["gate"] for f in pre["failures"]
                    if f["gate"] not in _JUDGE_GATES]
        # The judge gates are excluded above because only the SECOND verdict is
        # unknown in the pre-pass — the first is already in the bundle, and if
        # it is not "correct" no second opinion can rescue it (this is the
        # pre-existing skip condition, preserved).
        if (bundle.get("review") or {}).get("verdict") != "correct":
            blocking.append("judge_correct_twice (first verdict not correct)")
        if blocking:
            why = skip + ", ".join(sorted(set(blocking)))
            pre["second_judge"] = {"run": False, "reason": why}
            # Name the actual blockers, but never overwrite a reason the gate
            # itself produced (e.g. "first judge verdict 'plausible'") — that
            # is the evidence, and losing it is the pollution M4 is about.
            for f in pre["failures"] + pre["checked"]:
                if str(f.get("reason", "")).startswith("second judge not run:"):
                    f["reason"] = why
            return False, pre

        # Judge 1 read the issue verbatim; judge 2 reads it with injection and
        # steering spans redacted, so a single embedded steer cannot carry both
        # passes (audit M1). Divergence fails judge_correct_twice, as it should.
        n_title, k1 = autoship_gates.neutralize_issue_text(title)
        n_body, k2 = autoship_gates.neutralize_issue_text(body)
        second = judge_mod.review_patch(
            n_title, n_body, bundle.get("diff", ""),
            test_summary=(bundle.get("test") or {}).get("summary", ""))
        bundle["review2"] = second
        record = autoship_gates.evaluate(bundle, second_verdict=second, **common)
        record["second_judge"] = {"run": True, "neutralized_spans": k1 + k2}
        return record["pass"], record
    except Exception as e:
        return False, {"pass": False, "failures": [
            {"gate": "autoship_check_error",
             "reason": f"{type(e).__name__}: {str(e)[:180]}"}]}


def _restore(path, base, branch, env):
    """Return the checkout to a clean ``base`` and drop a temp branch.

    The ``branch -D`` here is retention hygiene, not tree hygiene: ``checkout
    -f base`` + ``clean -fdq`` already restore the tree completely. It is left
    exactly as it was — durability is provided by the pin ``prepare_commit``
    writes BEFORE this runs (see ``_pin_name``), not by keeping the branch.
    """
    _git(["checkout", "-q", "-f", base], cwd=path, env=env)
    if branch:
        _git(["branch", "-D", branch], cwd=path, env=env)
    _git(["clean", "-fdq"], cwd=path, env=env)
    _purge_appledouble(path, env)


# Durable-bundle pin namespace (design_bundle_durability_2026-09-03.md, O2).
#
# Deliberately NOT under refs/heads/. `prepare_commit` re-runs
# `checkout -B auto-fix/<n>` for the same issue, which silently MOVES a branch
# of that name — so a branch-based pin would let a re-run destroy the previous
# bundle while the queue still recorded its sha. A ref outside refs/heads/
# cannot be touched by `checkout -B`, is invisible to `git branch`, and is a
# GC root all the same.
_PIN_NS = "refs/omega/bundle"


def _pin_name(number, sha):
    """Pin ref for ONE bundle: ``refs/omega/bundle/<issue>-<sha12>``.

    Keyed on the commit, not just the issue. Keying on the issue alone was the
    first implementation and it failed its own test (P2c): `prepare_commit`
    reuses `auto-fix/<n>` across re-runs, so an issue-keyed pin MOVES on the
    second run and destroys the first bundle — the identical failure this
    design rejected O1 for, just relocated to a tidier namespace. The queue is
    append-only and still records the first sha, so that loss would surface
    later as an OBJECT GONE red against a row nobody had disposed of.

    The name is fully DERIVABLE from a queue row's (number, commit), which is
    what lets the digest verify pins on records written before `ship.pin`
    existed.
    """
    return f"{_PIN_NS}/{number or 'v2'}-{sha[:12]}"


def _summary_line(declared, issue, limit=66):
    """One-line summary, truncated at a WORD boundary so subjects don't end
    mid-sentence ("...during extraction and" — audit). limit=66 keeps
    'fix: ' + subject within the 72-char git convention."""
    s = ((declared or {}).get("summary") or issue.get("title")
         or "fix issue").strip().rstrip(".")
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return s


def prepare_commit(path, issue, changed_files, declared, env, *, dco=False):
    """Commit the agent's patch on a fresh branch *in this checkout*. Local only
    — no network. Returns (branch, base, commit_sha) or (None, base, reason).

    Stages ONLY the agent's changed files, so test-induced churn (go.sum /
    Cargo.lock rewritten by the affected-test run) never leaks into the commit.
    Clean human-style message, NO AI attribution (per feedback_no_coauthor). With
    ``dco`` a Signed-off-by trailer is added (the human contributor's DCO
    certification, required by some repos — not AI attribution).
    """
    base = _git(["rev-parse", "HEAD"], cwd=path, env=env).stdout.strip()
    number = issue.get("number") or 0
    branch = f"auto-fix/{number}" if number else "auto-fix/v2"
    _git(["checkout", "-q", "-B", branch], cwd=path, env=env)
    # Reset the index to base, then stage exactly the agent's files (covers
    # adds, modifications and deletions) — excludes lockfile churn from tests.
    _git(["reset", "-q", base], cwd=path, env=env)
    if changed_files:
        _git(["add", "--", *changed_files], cwd=path, env=env)
    summary = _summary_line(declared, issue)
    msg = f"fix: {summary}" + (f"\n\nRefs #{number}" if number else "")
    commit_cmd = ["commit", "-s", "-q", "-m", msg] if dco else \
        ["commit", "-q", "-m", msg]
    r = _git(commit_cmd, cwd=path, env=env)
    if r.returncode != 0:
        _restore(path, base, branch, env)
        return None, base, "commit_failed:" + (r.stderr or "")[:120]
    sha = _git(["rev-parse", "HEAD"], cwd=path, env=env).stdout.strip()
    # Durability pin, written BEFORE any caller can call `_restore`. That
    # `branch -D` is moments away and this branch is the ONLY ref to the commit
    # just built: without this line every review-lane bundle is created already
    # unreachable, surviving on reflog alone until the checkout's next `git gc`
    # (measured 2026-09-03: 12 of 25 recorded bundles were in exactly that
    # state, and the two oldest were already past the 30-day
    # gc.reflogExpireUnreachable window).
    #
    # Best-effort by design: a failed pin must not fail a good commit. The
    # digest fails closed on a missing pin (falls back to branch, then to a raw
    # object-existence check), so the cost of a silent failure here is a louder
    # digest, not a lost bundle.
    _git(["update-ref", _pin_name(number, sha), sha], cwd=path, env=env)
    return branch, base, sha


def ship_pr(repo, issue, path, changed_files, declared, env, *, do_push=False,
            reqs=None, ai_marker=None, ai_body=False):
    """Ship the agentic patch *from this checkout* — the correct, self-contained
    replacement for the legacy create_pr (which operates on contribution_loop's
    own CLONE_DIR and cannot ship a v2 patch).

    Injects repo contribution artifacts from ``reqs`` (changelog fragment, DCO
    sign-off, PR-template body) so the PR passes repo CI, not just compiles.

    Default (do_push=False): build and verify a real local commit, then restore
    the tree. No network, no PR. This is what the dry-run pipeline does.

    do_push=True (human-approved, outward): push the branch to the user's fork
    and open a ready-for-review PR via ``gh`` (drafts retired per user
    directive 2026-07-28 — approval to ship IS the readiness signal). Gated
    additionally by ALLOW_PR_CREATION.
    """
    reqs = reqs or {}
    # A raise ANYWHERE in ship_pr (changelog write, prepare_commit's git
    # calls, gh username timeout) must restore the checkout — the pre-commit
    # section used to sit outside any handler, leaving the tree on the
    # auto-fix branch for run_production's _reset_clean, which resets against
    # current HEAD and keeps the branch (audit v2.3.5).
    pre_base = _git(["rev-parse", "HEAD"], cwd=path, env=env).stdout.strip()
    try:
        return _ship_pr_inner(repo, issue, path, changed_files, declared, env,
                              do_push=do_push, reqs=reqs, ai_marker=ai_marker,
                              ai_body=ai_body)
    except Exception as e:
        try:
            _restore(path, pre_base,
                     f"auto-fix/{issue.get('number') or 'v2'}", env)
        except Exception:
            pass
        return {"status": "ship_error",
                "detail": f"{type(e).__name__}: {str(e)[:160]}"}


def _ship_pr_inner(repo, issue, path, changed_files, declared, env, *,
                   do_push, reqs, ai_marker, ai_body):
    # Add repo-required contribution artifacts to the patch before committing.
    #
    # ORDERING NOTE (audit BUG1-int): this runs AFTER the autoship gates, which
    # lint `bundle["diff"]` captured back in run_issue. So the changelog and
    # any other artifact written here are outside every content gate. That is
    # why `content_lint` needs no allowlist for the URL cosmos-sdk mandates in
    # a changelog bullet — the URL never reaches it. If diff capture is ever
    # moved after this point, content_lint and size_caps will start seeing the
    # changelog and both will need revisiting; do not "fix" the resulting
    # rejection by widening the lint without re-reading this comment.
    ship_files = list(changed_files)
    try:
        changelog_path = contribution_reqs.write_changelog(
            path, reqs, issue, declared.get("summary"),
            repo=repo, changed_files=changed_files)
    except contribution_reqs.ChangelogError as e:
        # A MANDATORY changelog could not be rendered. Stop before the commit
        # rather than ship a knowingly non-compliant PR — the failure mode this
        # whole design exists to remove (design §1A).
        #
        # This is the ONLY early return that precedes `prepare_commit`, so it is
        # also the only one not covered by a later `_restore`. Without this the
        # applied patch stays in the working tree and the NEXT issue in an
        # in-process batch (scripts/eval_v2_dryrun.py, validate_v2_slice.py)
        # inherits it (audit BUG4-int).
        _reset_clean(path, env)
        return {"status": "changelog_failed", "detail": str(e)}
    if changelog_path:
        ship_files.append(changelog_path)

    branch, base, info = prepare_commit(path, issue, ship_files, declared, env,
                                        dco=bool(reqs.get("dco")))
    if not branch:
        return {"status": "commit_failed", "detail": info}
    # Record the pin only after VERIFYING it resolves to the commit we are
    # about to advertise. `ship.pin` is a durability claim the digest trusts;
    # an unverified one would be the same failure this change exists to remove
    # (a record naming a referent that isn't there), just one level up.
    pin = _pin_name(issue.get("number") or 0, info)
    pinned = _git(["rev-parse", "-q", "--verify", pin + "^{commit}"],
                  cwd=path, env=env)
    pin_ok = pinned.returncode == 0 and pinned.stdout.strip() == info
    if not do_push:
        _restore(path, base, branch, env)
        return {"status": "prepared", "branch": branch, "commit": info,
                "pin": pin if pin_ok else None,
                "changelog": changelog_path, "dco": bool(reqs.get("dco")),
                "note": "local commit built & verified; push/PR gated (do_push=False)"}

    # ── outward path (deliberately not exercised by the dry-run pipeline) ──
    # Self-contained gating — no dependency on the legacy contribution_loop.
    # Default-OFF: opening PRs must be explicitly enabled.
    if os.getenv("ALLOW_PR_CREATION", "false").lower() != "true":
        _restore(path, base, branch, env)
        return {"status": "blocked", "detail": "ALLOW_PR_CREATION!=true"}
    user = _gh_username()
    if not user:
        _restore(path, base, branch, env)
        return {"status": "blocked", "detail": "no gh username"}
    # The whole outward section runs under try/finally so the local checkout is
    # ALWAYS restored (base + temp branch dropped) — even if a `gh`/`git` call
    # times out and raises. ship_pr is self-contained: it never propagates an
    # exception to run_production (which would clean the tree incorrectly).
    repo_name = repo.split("/")[-1]
    pushed_outward = False  # audit C4: post-push failures must be labeled as such
    try:
        # Ensure the user's fork exists and is wired as the `fork` remote — the
        # checkout's only remote is `origin` (= upstream). `gh repo fork` is idempotent.
        subprocess.run(["gh", "repo", "fork", repo, "--clone=false"],
                       capture_output=True, encoding="utf-8", errors="replace",
                       timeout=60, env=env)
        _git(["remote", "remove", "fork"], cwd=path, env=env)  # ignore if absent
        _git(["remote", "add", "fork",
              f"https://github.com/{user}/{repo_name}.git"], cwd=path, env=env)
        # Fetch the fork branch, then push with an EXPLICIT lease pinned to
        # the sha we just observed. Bare --force-with-lease after a fetch is
        # the documented git pitfall — the fetch refreshes the tracking ref so
        # the lease always passes, i.e. plain --force (audit v2.3.5). Pinning
        # the expectation keeps protection for anything pushed to the fork
        # branch after our fetch. A failed fetch (branch doesn't exist yet)
        # leaves an empty expectation = "must be absent", correct for a first
        # push.
        _git(["fetch", "-q", "fork", branch], cwd=path, env=env, timeout=60)
        expect = _git(["rev-parse", "-q", "--verify",
                       f"refs/remotes/fork/{branch}"], cwd=path, env=env)
        lease = (f"--force-with-lease={branch}:{expect.stdout.strip()}"
                 if expect.returncode == 0 and expect.stdout.strip()
                 else f"--force-with-lease={branch}:")
        # Push the branch to the fork, then open the PR from THIS checkout.
        pushed = _git(["push", lease, "-q", "fork",
                       f"{branch}:{branch}"], cwd=path, env=env, timeout=120)
        if pushed.returncode != 0:
            return {"status": "push_failed", "detail": (pushed.stderr or "")[:200]}
        pushed_outward = True
        title = "fix: " + _summary_line(declared, issue)
        # Honor the repo's AI-usage self-disclosure policy (e.g. terraform's 🤖🤖🤖
        # title marker, or caddy's PR-body disclosure section).
        title = contribution_reqs.disclose_ai_title(title, ai_marker)
        body = contribution_reqs.pr_body(issue, declared.get("summary", ""),
                                         ship_files, reqs, ai_disclose=ai_body)
        pr = subprocess.run(
            ["gh", "pr", "create", "--repo", repo, "--head", f"{user}:{branch}",
             "--title", title, "--body", body],
            cwd=path, capture_output=True, encoding="utf-8", errors="replace",
            timeout=60, env=env)
        url = pr.stdout.strip() if pr.returncode == 0 else ""
        if url and "github.com" in url:
            # Disposition reached: the PR is now the record, so the local pin
            # has done its job and is deleted (design §3 — pins are protective
            # only, deleted after ship/decline). Deliberately NOT done on the
            # push_failed / pr_create_failed / ship_error_postpush paths: those
            # end with the bundle's fate unknown, which is precisely when the
            # local commit still needs to exist.
            _git(["update-ref", "-d", pin], cwd=path, env=env)
            return {"status": "shipped", "pr_url": url, "branch": branch,
                    "changelog": changelog_path, "dco": bool(reqs.get("dco"))}
        # The push SUCCEEDED — this is a PR-create failure, a distinct state:
        # the branch is live on the fork and a PR may even exist (e.g. "a pull
        # request already exists"). Label it honestly so the operator doesn't
        # retry the push or double-open a PR (audit finding).
        return {"status": "pr_create_failed", "branch": branch,
                "detail": ((pr.stderr or "") + (pr.stdout or ""))[:200]}
    except Exception as e:
        # audit C4: an exception AFTER a successful push means a live branch
        # (and possibly a live PR, e.g. gh pr create client timeout) exists on
        # the fork — callers must treat it as an outward action, never as
        # "nothing happened".
        return {"status": ("ship_error_postpush" if pushed_outward
                           else "ship_error_prepush"),
                "detail": f"{type(e).__name__}: {str(e)[:160]}"}
    finally:
        _restore(path, base, branch, env)


# ── offline eval (ground truth) — integration check over the shared modules ──

def run_eval(issue_key, env=None, verbose=True):
    """Drive agentic_session + the calibrated comparison judge + the Stage-3
    differential over ONE ground-truth issue. Reproduces Stage 2+3 for a single
    issue through the production modules — the integration smoke test."""
    from scripts.benchmark_localization import prepare_checkout
    from scripts.benchmark_fix_agentic import human_diff

    env = env or env_guard.require(verbose=verbose)
    bench = {json.loads(l)["issue_key"]: json.loads(l)
             for l in open(os.path.join(_ROOT, "benchmark_issues.jsonl"))}
    iss = bench.get(issue_key)
    if not iss:
        print(f"unknown issue_key: {issue_key}")
        return {"outcome": "unknown_issue"}

    path = prepare_checkout(iss["repo"], iss["fix_sha"])
    if not path:
        return {"outcome": "checkout_failed"}

    # Stage 2: agentic fix vs human merged fix (calibrated comparison judge).
    diff, declared, err = agentic_fix(path, iss["issue_key"], iss["title"],
                                      iss["body"])  # reset=True (benchmark)
    h_diff = human_diff(path, iss["fix_sha"])
    verdict = judge_mod.compare_patches(iss["title"], iss["body"], diff, h_diff)

    # Stage 3: differential execution validation (control/patched/positive).
    fix_rec = {"agent_diff": diff, "agent_diff_chars": len(diff),
               "judge": verdict}
    lang = rh.issue_lang(iss["fix_files"])
    val = rh.validate_with_ground_truth(iss, fix_rec, lang)

    result = {"issue_key": issue_key, "agent_error": err,
              "compare_verdict": verdict.get("verdict"),
              "compare_action": judge_mod.gate(verdict.get("verdict", ""),
                                               comparison=True),
              "validation_outcome": val.get("outcome"), "lang": lang}
    print("\n=== eval ===")
    print(json.dumps(result, indent=2))
    return result


def _parse_args(args):
    """Flag-position-aware parsing: values of value-taking flags are NEVER
    interpreted as flags themselves — an issue title of literally `--push`
    or `--eval` must not change pipeline behavior (audit M1)."""
    value_flags = ("--repo", "--number", "--title", "--body-file", "--eval")
    flags, values = set(), {}
    i = 0
    while i < len(args):
        a = args[i]
        if a in value_flags and i + 1 < len(args):
            values[a] = args[i + 1]
            flags.add(a)
            i += 2
        else:
            flags.add(a)
            i += 1
    return flags, values


def main():
    flags, values = _parse_args(sys.argv[1:])
    if "--eval" in flags:
        res = run_eval(values.get("--eval", ""))
        return 1 if res.get("outcome") in ("unknown_issue", "checkout_failed") else 0
    if "--repo" in flags:
        repo = values.get("--repo", "")
        number = int(values["--number"]) if "--number" in values else 0
        title = values.get("--title", "")
        body = ""
        if "--body-file" in values:
            body = open(values["--body-file"]).read()
        issue = {"repo": repo, "number": number, "title": title, "body": body,
                 "issue_key": f"{repo}#{number}"}
        bundle = run_production(issue, ship="--ship" in flags,
                                push="--push" in flags)
        print(json.dumps({k: v for k, v in bundle.items() if k != "diff"},
                         indent=2, default=str))
        # Non-zero only on genuine pipeline failures (not on reject/no_fix,
        # which are valid outcomes).
        return 1 if bundle.get("outcome") in (
            "checkout_failed", "ship_error_prepush", "ship_error_postpush",
            "push_failed", "commit_failed", "pr_create_failed",
            "pipeline_error", "changelog_failed"
        ) else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
