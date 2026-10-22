# System architecture

Implementation-grounded description of Omega as of 2026-09-02. File paths
and names are the code's own. Claims here are **designed + tested** unless
marked otherwise; production-demonstrated behavior is catalogued separately
in `../evidence/production-record.md`.

## 1. Two planes

**Reasoning plane** — every place a language model runs. Each is a bounded
`claude -p` subprocess with an explicit tool allowlist and turn/time budget:

| Session | Module | Inputs | Tools | Output contract |
|---|---|---|---|---|
| Fix | `agent/agentic_session.py` (`agentic_fix`) | issue key/title/body (capped), repo checkout at HEAD | Read, Grep, Glob, Edit, Write — **no Bash** | edits in tree + one JSON line `{done, files, summary}`; empty diff + `done:false` = honest no-fix |
| Localize | same (`agentic_localize`) | issue text | read-only | file list (benchmark/localization use) |
| Judge pass 1 | `agent/judge.py` (`review_patch`) | issue text, diff, test summary | none (prompt-only) | verdict ∈ {correct, plausible, incorrect, insufficient} or infra verdicts {judge_timeout, judge_unparsed} |
| Judge pass 2 | same, invoked by `runner/pipeline_v2.py` | **neutralized** issue text (steering spans masked), same diff | none | same contract; divergence between passes fails the gate |
| Test generation | `agent/test_generation.py` | issue + fix context | bounded session | generated regression test or an explicit non-generation outcome |
| Benchmark judge | `judge.compare_patches` | agent diff vs merged human diff | none | equivalence verdict; used only in offline evaluation, never in shipping |

Model outputs are *data* to the control plane: parsed, validated, capped,
and never executed as instructions. Judge infra-failures are first-class
verdicts — a timed-out judge fails gates as "unjudged", never recorded as a
disagreement (audit M4).

**Control plane** — everything else. Deterministic Python/shell with no
model calls: scheduling, scoring, policy, gating, ledgers, git/gh
mechanics, reconciliation, measurement.

## 2. The daily chain

`cloud/monitor_daily.sh`, launched by launchd (`com.omega.cloud-monitor`,
09:17 daily). Exact order and gating semantics:

| # | Stage | Gates chain exit? | Notes |
|---|---|---|---|
| 0 | Chain-run guard | yes (refuses) | refuses a second chain if `logs/chain_running.pid` holds a live pid; writes marker + `OMEGA_CHAIN_PID`; trap-cleaned |
| 1 | GOMODCACHE mount | no | failure carried into summary as `gomodcache=DETACHED` |
| 2 | Capacity preflight | **yes — halts everything** | real write probes on every volume; df is not trusted (incident in-25) |
| 3 | Monitor | yes | fetch tracked PRs, diff notable events, write summary then state |
| 4 | Scout v2 | yes | discovery/ranking/queueing; scoring surface frozen (sha pin `cloud/scout_freeze.json`) |
| 5 | Feedback capture | no (600s timeout wrap) | maintainer-review capture into `cloud/feedback/`; ledger auto-committed |
| 6 | Scout v3 shadow sweep | no (900s timeout wrap) | shadow experiment only; writes confined to `cloud/scout_v3/**` |
| 7 | Executor | yes | the only stage that can cause outward actions |
| 8 | d3 durability | yes (lowest priority) | post-merge patch-survival bookkeeping; gates nothing else |

The chain exit code derives only from monitor/scout/executor/d3. The
desktop notification's "all stages green" additionally requires: mount ok,
zero unpushed commits (post-stage `git fetch` divergence check), the v2
freeze pin verifying, and the two non-gating stages returning 0 — non-gating
stages can never *fail silently into green* (audit P2-l).

Two more scheduled jobs run outside this chain: `presence_daily.sh`
(10:30 — integrity check, then discovery, then the reply watcher) and the
weekly Rust corpus collector (Mondays). An ops-digest job (11:00) derives
the operator's standing queue purely from authoritative state.

## 3. Candidate lifecycle (code path)

1. **Scout v2** (`cloud/scout_run.py` → `scout_repos.py`/`scout_issues.py`):
   repo scout *proposes* new repositories (operator must edit the config to
   admit); issue scout hard-filters (assigned, locked, excluded labels,
   stale, already-seen), scores additive evidence signals
   (maintainer-confirmed, repro, bug label, scope, recency, discussion),
   verifies top candidates against `agent/duplicate_check`, and appends at
   most 3/day to `scout_queue.jsonl` under the shared queue lock.
2. **Executor** (`cloud/executor_run.py`): startup reconciliation → breaker
   and blocked-reason halts → hygiene → budgets → per-repo capacity →
   candidate loop. Per candidate: approval check, per-repo autoship policy,
   already-processed check, capacity, **live refetch** (stale-closed /
   stale-assigned / label policy against live labels / injection lint over
   stored *and* fresh text / text-drift re-rank), then claim
   (`in_pipeline` + write-ahead intent persisted) and pipeline invocation
   with a per-run nonce.
3. **Pipeline** (`runner/pipeline_v2.py::run_production`): duplicate check
   → chain-guard + per-repo checkout flock → checkout at HEAD → repository
   intelligence (`agent/repo_profile.py` scans CONTRIBUTING + satellite AI
   policy files, cached by HEAD sha; `agent/contribution_reqs.py` detects
   changelog/DCO/CLA/template requirements) → fix session → scaffolding
   strip → affected-package tests → judge 1 → compliance gate → test
   generation → (autoship only) the gate wall with judge 2 → ship or
   queue-for-review. Every terminal outcome appends one JSON record to
   `pipeline_v2_queue.jsonl` carrying the executor's nonce.
4. **Ship** (`ship_pr`): fork sync, branch push with
   `--force-with-lease=<branch>:<observed-sha>`, `gh pr create` gated by
   `ALLOW_PR_CREATION=true`. Pre-push and post-push failures are distinct
   outcomes (`ship_error_prepush` is provably-no-outward;
   `pr_create_failed`/`ship_error_postpush` are outward-or-uncertain).
5. **Monitor** picks the PR up next morning; reviews/comments become
   notable events and feedback-ledger rows; merges update per-repo
   counters; an autoship PR closed unmerged trips the circuit breaker.

## 4. State and ledger inventory

| File | Writer(s) | Readers | Integrity |
|---|---|---|---|
| `scout_queue.jsonl` | scout (append), executor (merge-aware rewrite) | executor, v3 shadow, pilot | shared fcntl `QueueLock` + merge-on-write; whole-file rewrites carry forward rows appended since read |
| `pipeline_v2_queue.jsonl` | pipeline (append) | executor (nonce match), measurement | O_APPEND single-write records (empirically race-clean); readers skip torn lines |
| `cloud/repo_outcomes.json` | executor (`_write_outcomes`), preship tool | executor, gates, capacity | QueueLock + merge-aware union (intents, prs, third-slot justifications); `blocked_reason` never cleared by a stale writer |
| `cloud/monitor_state.json` | monitor | executor, capacity, reconcile | atomic tmp+replace; summary written **before** state so a crash duplicates events rather than losing them |
| `cloud/circuit_state.json` | executor | executor, gates | atomic; no code path writes `armed:true` |
| `ops_tracked_prs.json` / `cloud/monitor_targets.json` | executor sync + operator ships | monitor reconcile, capacity (third source) | atomic; monitor self-heals divergence each morning |
| `cloud/feedback/*.jsonl` | feedback capture (append-only) | measurement only | per-repo files; id-hash idempotence; capture lock; **no production module reads these** (pinned by test) |
| `cloud/scout_v3/state.json` + ledgers | shadow runner only | shadow/eval only | write-path guard confines all writes to the v3 tree; atomic fsync+replace |
| `presence/…events ledger` | `presence/ledger.append_event` | presence modules, integrity checker | append-only event sourcing; `LEGAL_IN`/`STATUS_AFTER` state machine validates every append; independent integrity re-verifier |
| `commercial/task_ledger.jsonl` | enroll/estimate tools | pilot report/integrity | append-only; enrollment timestamps are the anti-cherry-pick anchor |

## 5. Recovery and reconciliation

Summarized here; mechanism-by-mechanism treatment with the audit findings
that motivated each is in `../operations/failure-and-recovery.md`. The load
bearing ideas: write-ahead intents before any push-capable subprocess;
nonce-matched startup reconciliation of stuck claims; every
outward-or-uncertain condition (timeout, unknown outcome, missing record,
recovered outward-class failure) sets `blocked_reason` and halts autonomy
until an operator clears it; single-writer locks on all shared mutable
state; per-repo checkout locks; stale-safe pid guards everywhere a marker
or lock stores a pid.

## 6. Ancillary subsystems

- **Presence** (`presence/`): a separate contribution lane for non-code
  value (issue/discussion analyses, briefs, writeups) with its own
  event-sourced ledger, gate ladder (G0 policy → G1 structure → G2 novelty
  → G3 outward-text lint → G4 caps → G5 approval), per-item operator
  approval with sha-bound text, and a daily reply watcher on posted
  threads. Configured `zero_autonomous`: the config loader refuses any
  other mode.
- **Durability (d3)** (`cloud/d3_run.py`): tracks whether merged patches
  survive upstream (rewrites/reverts). Observational only; gates nothing.
- **Measurement stack**: scout v3 shadow evaluation, presence detector
  calibration, and the commercial pilot — see `../validation/`.
