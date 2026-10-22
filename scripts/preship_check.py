#!/usr/bin/env python3
"""Operator pre-ship concurrency check (repo-concurrency policy, 2026-08-03).

MANDATORY before every operator-approved ship (standing rule in
knowledge/repo_concurrency_policy.md): the auto path enforces the policy in
code via repo_health + gate 8; this is the equivalent enforcement point for
the human path. Read-only except --justify, which appends a third-slot
justification record to cloud/repo_outcomes.json.

    python3 scripts/preship_check.py --repo owner/repo
    python3 scripts/preship_check.py --repo owner/repo --justify \
        --pr-subject "..." --subsystems-differ --independent \
        --no-volume-concerns

Verdicts:
  ALLOW       open count < effective limit
  THIRD-SLOT  at the default limit (2); shipping requires --justify with ALL
              three condition flags AND a non-degraded repo (policy 2)
  BLOCK       degraded/cooldown repo at limit, or at the absolute ceiling (3),
              which only an explicit, recorded operator override may exceed
"""
import argparse
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cloud"))
sys.path.insert(0, ROOT)

import repo_health
import repo_metrics

QUEUE = os.path.join(ROOT, "pipeline_v2_queue.jsonl")


def _ts_key(rec, idx):
    """Sort key for queue records: parsed timestamp, else file order.

    Lexicographic compare of local-offset ISO strings fails open across a
    DST boundary (audit F2-audit 2026-09-02); parse when possible and let
    append order break ties / carry unparseable records.
    """
    raw = rec.get("outcome_ts") or ""
    try:
        return (datetime.datetime.fromisoformat(raw).timestamp(), idx)
    except ValueError:
        return (0.0, idx)


def latest_testgen(repo, issue, queue_path=None):
    """Latest pipeline record's test_generation for (repo, issue), or None.

    UNKNOWN ≠ PASS: callers treat a missing record exactly like a
    non-generated outcome (F2, patch-quality audit v2 2026-09-02).
    Records without a test_generation key (legacy shapes, early-exit
    outcomes) are skipped so a known outcome is never masked by a later
    record that simply never reached testgen.
    """
    best, best_key = None, None
    try:
        with open(queue_path or QUEUE) as f:
            for idx, ln in enumerate(f):
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue  # torn line, reader convention
                if (rec.get("repo") == repo and rec.get("number") == issue
                        and rec.get("test_generation")):
                    k = _ts_key(rec, idx)
                    if best_key is None or k >= best_key:
                        best, best_key = rec, k
    except OSError:
        return None
    return (best or {}).get("test_generation")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--justify", action="store_true",
                    help="record a third-slot justification (policy 2)")
    ap.add_argument("--pr-subject", default="",
                    help="what the third PR is (recorded)")
    ap.add_argument("--subsystems-differ", action="store_true")
    ap.add_argument("--independent", action="store_true")
    ap.add_argument("--no-volume-concerns", action="store_true")
    ap.add_argument("--issue", type=int,
                    help="issue number the prepared branch fixes; enables the "
                         "testgen check (F2) — mandatory on real pre-ship runs")
    ap.add_argument("--test-justify", default="",
                    help="recorded reason for shipping without a generated "
                         "test (required when testgen outcome != generated)")
    ap.add_argument("--capacity-only", action="store_true",
                    help="informational capacity query; skips the testgen "
                         "check VISIBLY (not valid for an actual ship)")
    args = ap.parse_args()

    # Chain guard + write lock (system audit P1-⑤, 2026-09-02): this tool's
    # read-modify-write of repo_outcomes.json raced the chain executor's
    # hours-long stale-snapshot rewrites — a --justify recorded mid-chain
    # could be silently erased. Refuse while the chain runs (stale-safe pid
    # check); the --justify write below additionally takes the shared lock,
    # and the executor's writes are now merge-aware, so entries survive
    # even a session-executor overlap.
    cpf = os.path.join(ROOT, "logs", "chain_running.pid")
    if os.path.exists(cpf):
        try:
            cpid = int(open(cpf).read().strip() or "0")
            alive = cpid > 0 and (os.kill(cpid, 0) or True)
        except (ValueError, OSError):
            alive = False
        if alive and os.getenv("OMEGA_CHAIN_PID") != str(cpid):
            print(f"REFUSED: daily chain is running (pid {cpid}) — retry "
                  "after the chain completes (audit P1-⑤)")
            return 1

    # Testgen visibility (F2, patch-quality audit v2): on the auto path a
    # missing generated test is a hard gate; here it becomes a recorded
    # operator decision instead of a silent default. The justification is
    # only WRITTEN on an ALLOW outcome (audit F2-audit 2026-09-02: rows for
    # ships that then blocked on capacity polluted the ledger).
    pending_test_row = None
    if args.capacity_only:
        if args.test_justify or args.justify:
            print("REFUSED: --capacity-only is an informational query — it "
                  "cannot record a justification (--justify/--test-justify).")
            return 1
        print("TESTGEN: SKIPPED (--capacity-only) — not valid for a ship")
    elif args.issue is None:
        print("REFUSED: pre-ship runs need --issue N for the testgen check "
              "(F2); use --capacity-only for informational queries.")
        return 1
    else:
        tg = latest_testgen(args.repo, args.issue)
        outcome = (tg or {}).get("outcome") or "UNKNOWN (no pipeline record)"
        print(f"testgen: {outcome}" +
              (f" — {tg['detail']}" if tg and tg.get("detail") else ""))
        if (tg or {}).get("outcome") != "generated":
            if not args.test_justify.strip():
                print("\nVERDICT: BLOCK — no generated regression test for "
                      f"{args.repo}#{args.issue}. Shipping without one "
                      "requires --test-justify \"<reason>\" (recorded); "
                      "UNKNOWN counts as absent (F2).")
                return 1
            pending_test_row = {
                "repo": args.repo, "issue": args.issue,
                "day": datetime.date.today().isoformat(),
                "testgen_outcome": outcome,
                "justification": args.test_justify.strip(),
                "pr_subject": args.pr_subject}

    def record_rows(extra_third_slot=None):
        if not pending_test_row and not extra_third_slot:
            return
        from queue_lock import QueueLock
        with QueueLock(repo_health.OUTCOMES):
            locked = repo_health.load_outcomes()
            if pending_test_row:
                locked.setdefault("test_absence_justifications",
                                  []).append(pending_test_row)
            if extra_third_slot:
                locked.setdefault("third_slot_justifications",
                                  []).append(extra_third_slot)
            repo_health.save_outcomes(locked)
        if pending_test_row:
            print("test-absence justification recorded in repo_outcomes.json")

    monitor_state = json.load(open(repo_health.MONITOR_STATE))
    outcomes = repo_health.load_outcomes()
    cfg = (json.load(open(os.path.join(ROOT, "cloud", "scout_config.json")))
           .get("executor") or {})
    cap, reasons = repo_health.capacity(args.repo, monitor_state, outcomes, cfg)
    open_counts, cr = repo_health._open_prs_by_repo(monitor_state, outcomes)
    n_open = open_counts.get(args.repo, 0)
    merged_cfg = {**repo_health.DEFAULTS, **cfg}
    absolute = merged_cfg["absolute_max_open"]
    default_max = merged_cfg["default_max_open"]
    degraded = reasons and reasons[0].startswith(("degraded", "cooldown"))

    m = repo_metrics.compute(monitor_state, outcomes, cfg).get(args.repo, {})
    print(f"repo: {args.repo}")
    print(f"open: {n_open}   effective limit: {cap}   regime: {reasons[0]}")
    for r in reasons[1:]:
        print(f"  - {r}")
    if m:
        print(f"metrics: merge_rate={m.get('merge_rate')} "
              f"cr_rate={m.get('change_request_rate')} "
              f"avg_d_to_merge={m.get('avg_days_to_merge')} "
              f"avg_d_to_activity={m.get('avg_days_to_first_activity')}")

    if n_open >= absolute:
        print(f"\nVERDICT: BLOCK — at the absolute ceiling ({absolute}). "
              "Only an explicit, recorded operator override may exceed it "
              "(policy 3). Distribute the work to another repo instead "
              "(policy 6).")
        return 1

    if degraded and n_open >= cap:
        print("\nVERDICT: BLOCK — repo is degraded/cooling and at its "
              "reduced limit; wait for confidence to recover (policy 4).")
        return 1

    if n_open < default_max and n_open < cap:
        record_rows()
        print("\nVERDICT: ALLOW")
        return 0

    # at the default limit: third slot, operator-only, needs justification
    conds = {"subsystems_differ": args.subsystems_differ,
             "independent": args.independent,
             "no_volume_concerns": args.no_volume_concerns,
             "no_changes_requested": args.repo not in cr,
             "capacity_healthy": not degraded}
    missing = [k for k, v in conds.items() if not v]
    if not args.justify:
        print(f"\nVERDICT: THIRD-SLOT — at the default limit ({default_max}). "
              "A third PR requires --justify with all condition flags "
              "(policy 2). Prefer another repo (policy 6).")
        return 1
    if missing:
        print(f"\nVERDICT: BLOCK — third-slot conditions not met: {missing} "
              "(policy 2).")
        return 1
    rec = {"repo": args.repo, "day": datetime.date.today().isoformat(),
           "pr_subject": args.pr_subject, "conditions": conds,
           "open_at_time": n_open}
    # Re-read under the shared lock so the appended justification lands on
    # CURRENT disk state, not this process's startup snapshot (audit P1-⑤);
    # one locked write carries both record classes on this ALLOW path.
    record_rows(extra_third_slot=rec)
    print("\nVERDICT: ALLOW (third slot) — justification recorded in "
          "repo_outcomes.json. This is the last slot; the absolute ceiling "
          f"is {absolute} (policy 3).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
