# Agent Omega

**An autonomous software-engineering system that discovers real open-source bugs,
generates verified fixes, and ships them upstream — with 11 merged pull requests in
production repositories including Caddy, rclone, go-ethereum, and k3s.**

`11 merged PRs · 22 tracked PRs across 11 repos · 16 fail-closed ship gates ·
1 fully-autonomous merged ship · 3,000+ commits`

Omega's design thesis: a coding model can produce plausible patches, but **reasoning does
not confer authority**. Every model output is a *proposal*; only a deterministic Python
control plane — budgets, per-repo policy, dual independent judges, sixteen fail-closed
gates, append-only ledgers, crash reconciliation — can cause an outward action. Any
condition that cannot be evaluated counts as failure, never permission.

> **About this repository.** This is a curated public snapshot of a private
> operational system. It contains the full architecture documentation, the core
> modules (pipeline, constrained agent sessions, judges, regression harness, ship
> gates, executor, scout, monitor), the benchmark suite, and a real end-to-end
> execution trace — selected for reading, not as a turnkey install. The operational
> state the live system runs on (queues, ledgers, per-repo policy records, daily
> reports, and the 3,000+ commit history) stays in the private repository; the
> *results* are independently verifiable through the public pull requests linked
> below.

---

## Architecture: the execution loop

```
  GitHub issues in approved repos
        │
        ▼
  SCOUT — issue discovery & ranking (cloud/scout_run.py)         deterministic
        │   hard filters (assigned/locked/stale) · evidence scoring · duplicate check
        ▼
  EXECUTOR — flow control, not judgment (cloud/executor_run.py)  deterministic
        │   daily budgets · per-repo capacity & policy · prompt-injection lint
        │   write-ahead ship intents (a crash can never free a ship slot)
        ▼   one candidate at a time
 ┌─────────────────── PIPELINE (runner/pipeline_v2.py) ───────────────────────┐
 │                                                                            │
 │  fresh checkout ──▶ [bug localization + fix generation]                    │
 │                      constrained `claude -p` session with file tools only  │
 │                      (Read/Grep/Glob/Edit/Write, NO shell) — explores the  │
 │                      repo, returns a diff plus a structured declaration    │
 │                      (or an honest "cannot fix confidently")               │
 │        │                                                                   │
 │        ▼                                                                   │
 │  affected-package tests on the patched tree                                │
 │        ──▶ [judge pass 1]  independent patch review                        │
 │        ──▶ [regression-test generation]  must FAIL on base, PASS on patch  │
 │        ──▶ [judge pass 2]  re-run on *neutralized* issue text — a steering │
 │                            phrase that fooled pass 1 produces a divergence │
 │        │                                                                   │
 │        ▼                                                                   │
 │  16 FAIL-CLOSED SHIP GATES (agent/autoship_gates.py)                       │
 │   any failure — or any gate that errors — routes to human review with      │
 │   the exact reasons recorded                                               │
 └────────────┬───────────────────────────────────────────────────────────────┘
              ▼  only if every gate passes
  branch push (force-with-lease, pinned to the observed sha)
  ──▶ ready-for-review PR on GitHub
              ▼
  MONITOR — daily PR tracking ──▶ maintainer reviews land in an append-only
  feedback ledger ──▶ capacity, circuit breaker, and durability updates

  [bracketed] = model reasoning (restricted tools, bounded input)
  everything else = deterministic control plane
```

The whole loop runs unattended as a daily launchd chain (09:17):
monitor → scout → feedback capture → executor → durability tracking.

## Proven outcomes — merged upstream contributions

All PRs are public and verifiable (statuses last verified 2026-10-21). Eleven merged
across five repositories:

| Repository | PR | Fix | Merged |
|---|---|---|---|
| caddyserver/caddy | [#7898](https://github.com/caddyserver/caddy/pull/7898) | caddyconfig: register nested named-route invokes transitively | 2026-07-30 |
| caddyserver/caddy | [#7905](https://github.com/caddyserver/caddy/pull/7905) | encode: flush headers immediately for SSE responses | 2026-07-31 |
| caddyserver/caddy | [#7904](https://github.com/caddyserver/caddy/pull/7904) | caddyauth: isolate provider responses against cross-provider clobbering | 2026-08-01 |
| caddyserver/caddy | [#7916](https://github.com/caddyserver/caddy/pull/7916) | reverseproxy: isolate active health-check state per check config | 2026-08-12 |
| caddyserver/caddy | [#7920](https://github.com/caddyserver/caddy/pull/7920) | caddyhttp: shield hostnames from a covering wildcard's client auth | 2026-08-12 |
| rclone/rclone | [#9639](https://github.com/rclone/rclone/pull/9639) | serve nfs: fix EIO when creating symlinks with `--vfs-links` | 2026-08-01 |
| rclone/rclone | [#9726](https://github.com/rclone/rclone/pull/9726) | drive: reuse resumable-upload chunk buffers via a size-matched pool | 2026-08-30 |
| rclone/rclone | [#9847](https://github.com/rclone/rclone/pull/9847) | onedrive: fall back to manual drive-ID entry when drive listing fails | 2026-09-01 |
| ethereum/go-ethereum | [#35433](https://github.com/ethereum/go-ethereum/pull/35433) | eth/syncer: only synthesize finalized/safe markers with an explicit sync target | 2026-08-12 |
| k3s-io/k3s | [#14552](https://github.com/k3s-io/k3s/pull/14552) | restore Flatcar detection in the install script | 2026-09-03 |
| esengine/DeepSeek-Reasonix | [#9707](https://github.com/esengine/DeepSeek-Reasonix/pull/9707) | merge live proxy-observed MCP tools into the capability catalog | 2026-09-25 |

- **One of those merges was fully autonomous**: DeepSeek-Reasonix #9707 was discovered,
  fixed, tested, gated, and shipped end-to-end with no human in the loop, then merged by
  the maintainer — the real execution trace is in
  [`examples/sample_execution_log.md`](examples/sample_execution_log.md). Every other
  ship was operator-reviewed on Omega-prepared work, per each repository's interaction
  policy.
- **Eight more PRs are open under review** in helm, cosmos-sdk, encore, huma, go-git,
  go-task, rclone, and caddy.
- **The negatives are documented too**: rclone #9642 was withdrawn after Omega's own
  analysis found the approach lossy (the design question was put to the maintainer), and
  two internal expansion experiments ended in pre-registered **NO-PROMOTE** verdicts.
  The measurement layer is built to issue NO-GO results, not just celebrate wins —
  see [`docs/validation/measurement-methodology.md`](docs/validation/measurement-methodology.md).

Full factual record and what it does/doesn't prove:
[`docs/evidence/production-record.md`](docs/evidence/production-record.md)
(dated 2026-09-02 — predates the k3s and DeepSeek-Reasonix merges above).

## Technical architecture

- **Reasoning plane** — constrained `claude -p` sessions (Read/Grep/Glob/Edit/Write, no
  shell, bounded input) for localization, fix generation, regression-test writing, and
  patch judging. Identical session code in production and in the benchmark harness.
- **Control plane** — deterministic Python: scheduler, budgets, per-repo policy records,
  sixteen ship gates evaluated at the push decision, circuit breaker on any autoshipped
  PR closed unmerged.
- **Execution environment** — macOS, scheduled via launchd (daily 09:17 chain with
  wake-from-sleep catch-up); heavy workloads (clones, Go/cargo caches) routed to an
  external SSD by a storage guard (`runner/env_guard.py`) that aborts if unmounted.
- **Git management** — per-repo checkout locks, branch pushes force-with-lease pinned to
  the observed sha, write-ahead ship intents persisted before any push-capable
  subprocess starts, startup reconciliation that matches stuck claims against an
  append-only record by nonce.
- **State tracking** — append-only JSONL ledgers (pipeline record, feedback ledger,
  ops history), fcntl single-writer locks and merge-aware writes on every shared state
  file, daily state diffs for tracked PRs.
- **Test-runner integration** — affected-package test runs on the patched tree
  (`agent/regression_harness.py`); generated regression tests must fail on base and
  pass on the patch, and survive an independent fix-blind validity judge.
- **Measurement** — a ground-truth benchmark suite (`scripts/benchmark_*.py`) retained
  as a permanent regression harness: agentic localization hits 88–93% top-1 file match
  (100% top-3) vs 11% for the keyword baseline it replaced; fix generation was validated
  against merged human fixes before going live.

## How it's operated

These are the real entry points on the production machine. Prerequisites there:
`python3`, authenticated [`gh`](https://cli.github.com/) CLI,
[Claude Code](https://claude.com/claude-code) CLI, Go toolchain. (This snapshot
includes the modules for reading; the operational state they consume — queues,
policy records, repo checkouts on the external SSD — stays private, so these
commands are documentation of the real workflow, not a turnkey install.)

```sh
# Run the pipeline on a single issue (dry-run by default: no PR is created
# unless ALLOW_PR_CREATION=true is explicitly set — the daily executor does
# this only on the live autoship path)
python3 runner/pipeline_v2.py --repo owner/name --number 1234 --title "issue title"

# Aggregate operational report (issues processed, verdicts, PR lifecycle)
python3 scripts/ops_report.py --refresh

# Pre-ship policy check for a repository (capacity, cooldowns, test justification)
python3 scripts/preship_check.py --repo owner/name --capacity-only
```

The full daily chain (monitor → scout → feedback capture → executor → durability
tracking) runs unattended via launchd at 09:17 — scheduling and recovery detail in
[`cloud/DEPLOY_LOCAL.md`](cloud/DEPLOY_LOCAL.md) and [`cloud/launchd/`](cloud/launchd/).
The repository allowlist and per-repo autonomy policy are operational state and
remain private.

## Honest limits

- Omega is not "fully autonomous": one narrow path is autonomous under sixteen gates and
  specific repos' policies; most shipped value moved through operator hands, and
  maintainers control every merge.
- A merged PR demonstrates that a change passed one real repository's review process —
  nothing more universal than that.
- The audit history ([`docs/audits/audit-history.md`](docs/audits/audit-history.md)) is
  a record of real defects found in Omega itself, each remediated with regression tests.

## How this system was built

Built over ~7 months (3,000+ commits) by a single operator using **Claude Code as a
major implementation and reasoning component**: the operator directed the architecture,
set the acceptance criteria and policy boundaries, and drove daily operation and audits;
Claude Code performed substantial implementation, debugging, and test-generation work
under that direction. This repository discloses that plainly — the same disclosure
standard Omega applies to its upstream contributions.

## Documentation map

| Document | Contents |
|---|---|
| [`examples/sample_execution_log.md`](examples/sample_execution_log.md) | Real end-to-end execution trace: issue → fix → tests → gates → merged PR |
| [`docs/overview.md`](docs/overview.md) | Full system overview: authority model, candidate lifecycle, failure & recovery |
| [`docs/architecture/system-architecture.md`](docs/architecture/system-architecture.md) | Component/data-flow detail; deterministic vs reasoning split; state inventory |
| [`docs/architecture/autonomy-and-authority.md`](docs/architecture/autonomy-and-authority.md) | Gates, repo policy, capacity, fail-closed doctrine |
| [`docs/operations/failure-and-recovery.md`](docs/operations/failure-and-recovery.md) | Recovery mechanisms: scenario → danger → control → invariant → tests |
| [`docs/operations/maintainer-feedback.md`](docs/operations/maintainer-feedback.md) | The maintainer-feedback ledger |
| [`docs/validation/measurement-methodology.md`](docs/validation/measurement-methodology.md) | Anti-self-deception measurement design |
| [`docs/evidence/production-record.md`](docs/evidence/production-record.md) | The factual PR record and what it proves |
| [`docs/evidence/traceability.md`](docs/evidence/traceability.md) | Evidence chain from issue to outcome |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Historical snapshot of the v2.3.2 pipeline freeze and the evidence chain behind v2 |

## License

[MIT](LICENSE)
