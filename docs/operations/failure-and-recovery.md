# Failure, recovery, and reconciliation

The failure modes Omega is designed to survive, documented through the
mechanism that handles each. Most controls here were introduced or
hardened by the September 2026 audits (operator system audit + 38-agent
fleet audit); each entry cites its finding. Format per mechanism:
**scenario → dangerous behavior if unmanaged → control → invariant →
test coverage.**

## 1. Executor timeout with uncertain outward state (fleet P1-①)

- **Scenario**: the pipeline subprocess exceeds its timeout while — worst
  case — a `git push`/`gh pr create` is in flight at the very end of the
  run.
- **Unmanaged**: the original branch overwrote the claim's status,
  removing it from reconciliation's view, set no `blocked_reason`, and
  let autonomy resume next day with a possibly-live PR that existed in no
  ledger — invisible to the breaker, the blindness check, and capacity.
- **Control** (`cloud/executor_run.py`, TimeoutExpired branch): the claim
  **stays `in_pipeline`**, `blocked_reason` is set (outward-or-uncertain
  wording), the day aborts; the subprocess runs with
  `start_new_session=True` so its process group is isolatable.
- **Invariant**: a timeout is treated exactly like the no-record case —
  no autonomous action anywhere until an operator verifies the fork/PR
  state and clears the block.
- **Tests**: `scripts/test_executor_main.py` P1-① trio (timeout sets
  block; claim preserved; next run halts).

## 2. Startup reconciliation (audit C5 + fleet P2-a)

- **Scenario**: the executor died mid-run; claims sit `in_pipeline`.
- **Unmanaged**: shipped PRs untracked; worse, a *recovered*
  outward-class failure (`push_failed` etc.) previously resolved its
  intent and let the day continue — a crash could hide an outward
  failure from the never-auto-retried rule.
- **Control**: every `in_pipeline` claim is matched against
  `pipeline_v2_queue.jsonl` by per-run nonce. Shipped → synced into
  tracking; no record → `blocked_reason` (an outward action may have
  occurred without a trace); recovered outward-class outcome → intent
  resolved *and* `blocked_reason` set.
- **Invariant**: no claim outcome is ever silently lost; uncertainty
  always lands on the operator, never on a retry.
- **Tests**: executor suites' reconciliation cases; recovered-outward
  behavior added with the P2 batch (`c916542`).

## 3. Merge-aware outcomes writes (fleet P1-⑤)

- **Scenario**: the executor holds `repo_outcomes.json` in memory for up
  to an hour while `preship_check --justify` (the operator's mandated
  pre-ship tool) does its own read-modify-write.
- **Unmanaged**: a whole-file write from the stale snapshot silently
  erased the operator's concurrent entries — undercounting the daily
  outward cap the gates read from disk.
- **Control**: all ten executor write sites route through
  `_write_outcomes`: under the shared fcntl `QueueLock`, disk-only
  intents, PRs, and third-slot justifications are unioned in, and an
  in-memory `None` never clears a disk `blocked_reason`. `preship_check`
  additionally refuses to run mid-chain and re-reads under the lock.
- **Invariant**: no writer of the outcomes ledger can destroy another
  writer's rows; budget arithmetic sees every intent.
- **Tests**: `test_executor_main.py` P1-⑤ (disk-only PR, intent, block
  reason, justification all survive a stale-snapshot rewrite).

## 4. Queue single-writer + merge-on-write (fleet P1-1)

- **Scenario**: scout appends a candidate while the executor rewrites
  `scout_queue.jsonl` wholesale from its startup snapshot.
- **Unmanaged**: the appended row vanished forever — scout's seen-keys
  state already recorded the issue, so it would never re-queue. Silent,
  permanent candidate loss.
- **Control**: `cloud/queue_lock.py` (fcntl, blocking) around both
  writers; the executor's rewrite re-reads the file inside the lock and
  carries forward rows it has never seen (belt-and-braces: a lock bug
  degrades to the old race, not silent loss).
- **Invariant**: a row appended by any writer survives any other
  writer's rewrite.
- **Tests**: `test_executor_main.py` P1-1 (foreign row survives).

## 5. Checkout locking + chain guards (fleet P1-⑥, P2-1, P2-c)

- **Scenario**: a manual pipeline or stage run overlaps the daily chain;
  or a second full chain starts while one is live.
- **Unmanaged**: two pipeline runs share one per-repo working tree —
  `capture_diff` records *whatever is in the tree*, so an overlap could
  ship a contaminated diff; manual stage runs interleave state writes;
  a second chain overwrote the live guard marker.
- **Control**: `run_production` takes a non-blocking per-repo flock on
  the checkout (loser gets `checkout_failed`, already a safe outcome
  class) plus a chain guard (chain-spawned pipelines inherit
  `OMEGA_CHAIN_PID` and pass; manual runs mid-chain refuse). Monitor,
  scout, and preship carry the same guard; the chain itself refuses to
  start over a live marker.
- **Invariant**: one writer per working tree; one chain at a time; no
  session-driven run interleaves a live chain.
- **Tests**: `test_autonomy_audit.py` A16b/A16c; live-verified guard
  behavior (manual refused / chain passes / dead pid ignored).

## 6. Stale PID handling (fleet P2-d + watcher audits)

- **Scenario**: SIGKILL (the chain's own timeout wrappers kill that way)
  orphans a pid-bearing lock or marker; or the file is empty.
- **Unmanaged**: `os.kill(0,0)` signals the caller's own process group
  and *succeeds* — an empty marker read as pid 0 looked "alive" forever;
  an orphaned lock blocked every future run of its subsystem.
- **Control**: every liveness probe requires pid > 0 before `kill -0`
  (shell and Python sites); every lock holder writes its pid and every
  acquirer breaks dead-pid locks loudly.
- **Invariant**: no lock or guard outlives its process.
- **Tests**: stale-lock/dead-pid cases in the v3, feedback, and followup
  suites; guard tests in `test_autonomy_audit.py`.

## 7. Kill-safe state commits (v3/feedback audits)

- **Scenario**: SIGKILL lands mid-write on a state file, or between a
  ledger append and the state commit.
- **Unmanaged**: a truncated `state.json` parsed as "no state" and
  silently zeroed experiment counters; a consumed-day marker written
  before the report left a day spent with no measurement.
- **Control**: tmp + fsync + `os.replace` for state files; state commits
  **last** (after reports), with idempotent per-(day,key) ledger appends
  so the re-run the design invites cannot double-write; the same-day
  sweep guard makes duplicate invocations spend-free no-ops.
- **Invariant**: state files are always the old or the new version,
  never torn; a crashed run is re-runnable without double-counting.
- **Tests**: crash-recovery simulation in the v3 suite (run → delete
  state → re-run → exactly one row, one sweep).

## 8. Autostash detection (system P1-3/P2-i)

- **Scenario**: stage `git pull --rebase --autostash` hits a conflicted
  stash application.
- **Unmanaged**: git parks the changes in the stash and moves on — a
  freshly written state file could sit stranded while the run reports
  success; previously, the *plain* rebase simply failed daily on the
  dirty tree and origin was silently never integrated.
- **Control**: `--autostash` on all three stage pulls plus a post-pull
  `git stash list` check that shouts `AUTOSTASH STRANDED` into the log;
  the chain separately fetches origin afterwards and surfaces any
  unpushed-commit count in the summary and RED banner.
- **Invariant**: local/origin divergence is loud, never ambient.

## 9. Fail-closed configuration (fleet P1-②, P2-b)

- **Scenario**: the gate-evaluation config can't be read; or a test
  invocation redirects state (`--state-dir`) while the config says live.
- **Unmanaged**: `except: cfg = {}` made the two *policy* gates pass
  vacuously — fail-open at the declared enforcement point; a
  `--state-dir` run could take real outward actions gated by sandbox
  breaker/ledger state.
- **Control**: the config read now propagates into `_autoship_check`'s
  outer fail-closed handler (unreadable config ⇒ queued_for_review);
  `--state-dir` forces dry-run unless the test harness explicitly opts
  in via env.
- **Invariant**: broken inputs can only block a ship, never enable one.
- **Tests**: `test_autonomy_audit.py` A16 (functional: nonexistent
  config path ⇒ fail-closed record); `--state-dir` covered by the
  harness opt-in design.

## 10. Idempotency as a general property

Duplicate protection is uniform across the system: pipeline records are
nonce-matched; feedback rows are content-hash keyed and re-ingestion
returns `duplicates_skipped`; v3 sweeps are one-per-day with pre-spend
refusal; shadow/drop ledgers dedupe per (day, key); the resolution cache
in the ops digest never un-ships a SHIPPED verdict; ledger appends in
presence validate against the state machine so illegal repeats are
refused at the boundary.

## Historical incidents (labeled historical)

The doctrine above was not designed in the abstract. Selected incidents
that shaped it: the k3s #14018 near-duplicate ship (stale index caught by
the pre-ship competing-PR check → shipped-detection automation); the
exFAT `._*`/timestamp incidents (in-24) that motivated env_guard and the
GOMODCACHE migration; the sparsebundle capacity lie (in-25) that
motivated the write-probe preflight; the encore #2187 intent race that
motivated the prospective intent gate; and the tidb #70636 transient-auth
permanent-retirement that produced the non-conclusion outcome class.
Details: `knowledge/incidents.md` and the design docs referenced there.
