# Omega — system overview

> Historical system overview (status statements dated **2026-09-02**), preserved
> verbatim when the root `README.md` was rewritten in 2026-10. PR counts below are
> stale — the current factual record is
> [`docs/evidence/production-record.md`](evidence/production-record.md).

**An operator-governed autonomous system for contributing verified fixes to
real open-source repositories** — discovery through shipped pull request
through maintainer outcome — built so that every outward action is earned
from a deterministic control plane, never assumed by an AI reasoning layer.

This document is the conceptual map. Implementation-grounded detail lives in
[`docs/`](.); per-repo operational knowledge lives in the private operational
repository's `knowledge/` records (not part of this public snapshot).

---

## The problem

Modern coding agents can produce plausible patches at scale. The hard part
is everything around that: knowing which issues are worth touching, whether
a patch is actually correct rather than plausible, whether a repository's
contribution process permits acting at all, when an autonomous system may
open a PR without a human in the loop, and how to prove — to an auditor, a
maintainer, or the operator — why any given action happened. Omega is an
attempt to build that surrounding machinery honestly: a system that ships
real fixes upstream while treating its own competence as a hypothesis to be
measured, not a premise.

## The central principle: reasoning does not confer authority

Omega separates two planes:

- The **reasoning plane** — Claude sessions (`claude -p`) that localize
  bugs, write fixes, generate regression tests, and judge patches. These
  components are powerful and fallible. They run with restricted tools, on
  bounded inputs, and their outputs are *proposals*.
- The **control plane** — deterministic Python and shell: schedulers,
  budgets, per-repo policy, fifteen fail-closed ship gates, append-only
  ledgers, circuit breakers, locks, and reconciliation. Only this plane can
  cause an outward action, and it treats every reasoning output, every
  external text, and every unreadable input as untrusted.

Nothing the reasoning plane says can open a PR. A patch reaches GitHub only
when the deterministic plane independently verifies every condition — and
any condition that *cannot be evaluated* (unreadable config, failed fetch,
unknown outcome string) counts as failure, not as permission. The recurring
doctrine in code comments is literal: **UNKNOWN is never PASS**.

## Architecture at a glance

```
                     launchd 09:17 daily chain (cloud/monitor_daily.sh)
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ chain guard → capacity preflight → MONITOR → SCOUT v2 →                 │
  │ feedback capture → [scout v3 shadow sweep] → EXECUTOR → durability (d3) │
  └─────────────────────────────────────────────────────────────────────────┘
        │                                   │
        │ tracked-PR states,                │ candidates (scout_queue.jsonl)
        │ maintainer events                 ▼
        │                        ┌─────────────────────────────┐
        │                        │ EXECUTOR (deterministic)    │
        │                        │ budgets · capacity · policy │
        │                        │ labels · injection lint ·   │
        │                        │ write-ahead intents         │
        │                        └──────────┬──────────────────┘
        │                                   │ one candidate at a time
        │                                   ▼
        │                        ┌─────────────────────────────┐
        │                        │ PIPELINE (runner/pipeline_v2)│
        │                        │ dup-check → repo profile →  │
        │                        │ [fix session] → tests →     │
        │                        │ [judge 1] → [testgen] →     │
        │                        │ [judge 2, neutralized] →    │
        │                        │ 15 SHIP GATES → ship_pr     │
        │                        └──────────┬──────────────────┘
        │                                   │ only if every gate passes
        ▼                                   ▼
   maintainer reviews  ◄───────────  PR on GitHub
   (feedback ledger)                 [ ] = model reasoning; everything else deterministic
```

Major components (each detailed in `docs/architecture/`):

| Component | Role | Nature |
|---|---|---|
| Monitor (`cloud/monitor_run.py`) | Daily fetch of every tracked PR; diffs state into notable events (merges, reviews, CI) | deterministic |
| Scout v2 (`cloud/scout_*.py`) | Repository proposal + issue discovery/ranking into a candidate queue; scoring surface frozen under a sha pin | deterministic |
| Scout v3 (`cloud/scout_v3/`) | Shadow-mode successor under a pre-registered evaluation; writes nothing production consumes | deterministic |
| Executor (`cloud/executor_run.py`) | Flow control, not judgment: budgets, capacity, policy checks, claims, ledger, report | deterministic |
| Pipeline (`runner/pipeline_v2.py`) | Per-candidate fix → verify → judge → gate → ship | mixed (bracketed stages are model sessions) |
| Ship gates (`agent/autoship_gates.py`) | Fifteen fail-closed conditions evaluated at the push decision | deterministic |
| Presence (`presence/`) | Non-code contribution lane (analyses, briefs) with its own gate ladder and a zero-autonomous outward wall | mixed |
| Feedback ledger (`cloud/feedback_capture.py`) | Captures maintainer review text as classified, append-only evidence | deterministic |
| Pilot (`commercial/`) | Frozen 30-task measurement cohort with blinded pre-estimates | deterministic |

## How a candidate moves through the system

1. **Discovery.** Scout scans approved repositories, hard-filters
   (assigned/locked/stale/duplicate), scores evidence signals, verifies
   against a duplicate check, and queues at most a few candidates per day.
2. **Pre-flight.** The executor re-fetches the live issue: closed or
   assigned → stale; title/body changed → operator re-rank; injection
   heuristics over stored *and* fresh text; per-repo label policy checked
   against the live label state.
3. **Fix.** A bounded Claude session (read/grep/edit tools, no shell)
   receives only the issue and the checkout, and produces a diff plus a
   structured declaration — or an honest "cannot fix confidently".
4. **Verification.** Affected package tests run. A regression test is
   generated in a separate session; autoship requires that generation
   succeed. Two independent judge passes review the patch — the second on
   *neutralized* issue text, so a steering phrase that fooled the first
   pass produces a divergence, which fails the gate.
5. **The gate wall.** All fifteen gates evaluate at the push decision.
   Any failure — or any gate that errors — routes the candidate to
   `queued_for_review` with the exact reasons recorded for the operator.
6. **Ship.** Only with `OMEGA_AUTOSHIP=1` and `ALLOW_PR_CREATION=true`
   (set solely by the executor, only when live) does `ship_pr` push a
   branch (force-with-lease pinned to the observed sha) and open the PR.
7. **Afterlife.** The monitor tracks the PR daily; maintainer reviews and
   comments land in the feedback ledger; merges/closures update capacity,
   the circuit breaker, and the durability tracker.

## Repository policy governs autonomy

Approval to *discover* in a repository is never approval to *ship* to it.
Each admitted repo carries an audited policy record
(`knowledge/repos/<repo>.md` + `cloud/scout_config.json`):

- **Autonomous** — the full path above may run unattended. As of
  2026-09-02, two repositories: `cli/cli` (with a machine-enforced
  requirement that issues carry the `help wanted` label, checked against
  live GitHub state at both act time and ship time) and
  `esengine/DeepSeek-Reasonix`.
- **Operator-gated** — six repositories (caddy, k3s, go-task, cilium,
  authelia, hister) where autoship is off by policy: mostly because the
  *maintainers* require human-authored interaction, plus one operator
  security ruling. Omega investigates and prepares; a human reviews to
  comprehension, ships, and writes every reply.
- **Everything else** admitted on normal terms defaults to
  autoship-permitted but is still bound by every gate, budget, and
  capacity rule.

Cross-cutting caps apply everywhere: at most 2 outward ships per day, 3
pipeline attempts per day, 2 open PRs per repo (degraded to 1 on requested
changes or an unmerged autoship PR; 14-day freeze after a close-unmerged; a
circuit breaker halts all autoshipping if any autoshipped PR is closed
unmerged, and stays halted until the operator acknowledges).

## Failure and recovery, briefly

The design assumption is that crashes, kills, and partial failures happen
*during* outward actions. The core mechanisms (full detail with the audit
findings that motivated them in `docs/operations/failure-and-recovery.md`):

- **Write-ahead intents**: an intent is persisted *before* any
  push-capable subprocess starts; an unresolved intent burns its daily
  slot forever, so a crash can never free budget.
- **Outward-or-uncertain**: a pipeline timeout, an unknown outcome string,
  or a missing record are all treated as "a PR may exist without a trace"
  — the day halts with a `blocked_reason` only an operator can clear.
- **Startup reconciliation** matches every stuck claim against the
  pipeline's append-only record by nonce, recovering ships and blocking on
  uncertainty.
- **Single-writer discipline**: fcntl locks and merge-aware writes on
  every shared state file; per-repo checkout locks; a chain-run marker
  that refuses overlapping manual runs.

## Measurement without self-deception

Omega's history includes shipping a system whose "success rate" turned out
to measure nothing (the v1 system below), so the current measurement layer
is built adversarially against its own optimism: pre-registered acceptance
bars that cannot move after seeing results, temporal train/test splits,
sha-frozen feature vocabularies and model artifacts, one-shot evaluations
that consume themselves, blinded human estimates with an enforced
before-any-run rule, intent-to-treat reporting that refuses to emit if any
enrolled task is missing, and explicit NO-GO verdicts that were actually
issued (a Rust language expansion was closed **NO-PROMOTE** on its
pre-registered bars; a promising test-generation change measured a real
improvement and was **reverted anyway** because its registered criteria
failed). Full methodology: `docs/validation/measurement-methodology.md`.

## Production status (as of 2026-09-02 — stale, see note at top)

- **21 tracked upstream PRs across 10 repositories.** Nine merged
  (caddy ×5, rclone ×3, go-ethereum ×1), two closed unmerged, the rest
  open under review. One PR shipped fully autonomously through all
  fifteen gates (DeepSeek-Reasonix #9707, 2026-09-01, open with CI green);
  every other ship was operator-executed on Omega-prepared work.
- **Presence** has three published non-code contributions (two issue
  analyses, one discussion analysis on etcd), each individually
  operator-approved with the exact text bound at approval.
- **Live experiments**: scout v3 shadow evaluation (3 of 14 required
  sweeps; zero of 20 required outcomes; no interpretation until the
  one-shot eval), the 30/30 frozen pilot cohort (passive accumulation),
  and the maintainer-feedback ledger (35 captured lessons, one already
  demonstrably applied in a later rework).
- **Validation state**: a 38-agent independent adversarial audit (six
  lenses) plus operator-driven audits found 6 P1 and 12 P2 root causes in
  September; all are remediated with regression tests
  (commits `c925759`, `c916542`), a ~13-suite battery is green, and the
  first full production day over the remediations ran clean.

## What Omega does not claim

- **Not "fully autonomous."** One narrow path is autonomous under fifteen
  gates and two repos' policies; the majority of shipped value to date
  moved through operator hands, and maintainers control every merge.
- **Not bug-free or guaranteed correct.** The audit history in
  `docs/audits/` is a record of real defects found in Omega itself,
  including ones that reached production behavior before being caught.
- **Not proven superior to human engineers.** The 2026-07 blinded
  comparison found parity on fix correctness and a *clear deficit* on
  test quality (0 of 33 pairs favored Omega). A pre-registered re-run on
  the post-testgen cohort (2026-09-02, n=14) found **partial movement**:
  the test-quality median rose from 1 to 3.5 and the July score ceiling
  broke, but test presence was 57% against an 80% bar, and that run's
  blinding failed (93% machine-arm identification), downgrading its
  agent-vs-human comparisons to descriptive. Both results are documented,
  not hidden (`knowledge/patch_quality_audit_v2_2026-09.md`).
- **A merged PR demonstrates** that a change passed one real repository's
  review process — nothing more universal than that.

Throughout the docs, claims are tagged by strength: **designed** (the code
exists and is tested), **tested** (verified by suites/fixtures), and
**demonstrated** (observed in production against the real world).

## How this system was built

Omega was developed over roughly four to five months (first commit
2026-03-30; 3,110 commits) by a single operator with no formal software
engineering or computer science training, using **Claude Code as a major
implementation and reasoning component**. The accurate division of labor:
the creator conceived and directed the architecture, established the system
boundaries and acceptance criteria, made every consequential policy and
risk decision, operated the system daily, and drove its audits and
iterative improvements. Claude Code performed substantial implementation,
debugging, investigation, and test-generation work under that direction.
The creator did not personally write every line of code, and this
repository does not pretend otherwise — the same disclosure standard Omega
applies to its upstream contributions.

**Historical note:** everything before 2026-06 was a different system (a
multi-domain "v1" spanning keyword-driven contributions, content
generation, and blockchain tooling). A ground-truth backtest showed v1 had
never shipped a PR and localized the wrong file 7 of 7 times; it was
discarded and the current agentic v2 architecture built in its place. The
old `ARCHITECTURE.md` at the repo root is retained as a historical
snapshot of the v2.3.2 freeze; current architecture documentation lives in
`docs/architecture/`.

## Documentation map

| Document | Contents |
|---|---|
| `docs/architecture/system-architecture.md` | Full component/data-flow detail; deterministic vs reasoning table; state and ledger inventory |
| `docs/architecture/autonomy-and-authority.md` | Authority model: gates, repo policy, labels, capacity, fail-closed doctrine |
| `docs/operations/failure-and-recovery.md` | Every recovery mechanism: scenario → danger → control → invariant → tests |
| `docs/operations/maintainer-feedback.md` | The feedback ledger: capture, classification, assessment; current scope |
| `docs/validation/measurement-methodology.md` | Anti-self-deception mechanisms and why each exists; the H1 case study |
| `docs/validation/scout-v3-experiment.md` | Discovery scoring, v2 freeze, v3 shadow evaluation |
| `docs/validation/pilot.md` | The frozen 30-task pilot: blinding, integrity, reporting rules |
| `docs/evidence/traceability.md` | The evidence chain from issue to outcome; reconstructability |
| `docs/evidence/production-record.md` | The factual PR/post record and what it does and does not prove |
| `docs/audits/audit-history.md` | Chronology of audits, findings, and remediations with commits |
