# Architecture

## Status — FROZEN at v2.3.2 (2026-07-02)

The production architecture is **frozen** at **v2.3.2** (git tag `v2.3.2`, commit `9c7e689`).
No new production capabilities are added unless justified by a **production incident**, a
**benchmark regression**, or **maintainer feedback**. The project's focus is now **operation**, not
architecture expansion.

**Freeze exception — v2.4.0 (2026-07-28, git tag `v2.4.0`):** regression-test generation is
ENABLED as a user-approved, evidence-validated exception (mechanical audit + 4/4 blind historical
replay, see `knowledge/v24_replay.md`). A fix-blind agent (Read/Grep/Glob/Write, no Edit/Bash)
writes a regression test on the base tree after the judge stage; the test must fail on base, pass
on the patched tree, survive an independent fix-blind validity judge, and lint clean. It is
fail-closed the safe way around: any outcome other than `generated` restores the tree to fix-only
and never blocks the fix, but auto-ship gate 6 (`knowledge/autoship_policy.md`) requires
`generated` — everything else routes to operator review. Controlled by `OMEGA_TESTGEN` (default on).

Operational observability (read-only, no production capability): **`scripts/ops_report.py`**
aggregates the artifacts the pipeline already emits (`pipeline_v2_queue.jsonl`, `repo_profiles/*.json`,
and — with `--refresh` — live PR lifecycle via the read-only `scripts/pr_monitor.py`) into
`ops_report.md` plus a `ops_history.jsonl` time-series. It tracks: issues processed, investigations,
duplicate skips, compliance stops, fixes generated, review verdicts, PRs opened + states,
requested-changes / approvals / merges / closes, runtime, recurring failure modes, and maintainer
feedback.

The production pipeline: `env_guard → duplicate check → repository intelligence/compliance → agentic fix
→ scaffolding strip → affected tests → review judge → regression-test generation (v2.4) → compliance
gate → human-gated ship → PR monitoring & learning`.

Daily scouting layer (2026-07-30, user-directed ops-layer addition — not a pipeline freeze
exception): `cloud/scout_run.py` (+ `scout_gh.py`, `scout_repos.py`, `scout_issues.py`,
`scout_config.json`, `scout_state.json`) chains after the 09:17 monitor in
`cloud/monitor_daily.sh`. Deterministic and read-only against GitHub: the repo scout searches
configured ecosystems and scores responsiveness/activity/backlog/CI/engagement, PROPOSING repos
in the daily report (`cloud/scout/YYYY-MM-DD.md`) for the operator to add to the
`approved_repos` allowlist; the issue scout filters and ranks issues in approved repos
(hard filters + confidence signals + `agent/duplicate_check.py` verification) and queues the
top candidates in pipeline-ready form to the gitignored `scout_queue.jsonl`. The scout never
executes the pipeline and removes no gate.

Freeze exception — v2.5.0 (2026-07-31, git tag `v2.5.0`, user-directed controlled auto-ship
experiment): the push decision point in `runner/pipeline_v2.py` now enforces EVERY auto-ship
gate in code when `OMEGA_AUTOSHIP=1` (set only by `cloud/executor_run.py`, the third step of
the daily launchd chain): second independent judge pass (both must be `correct` — `plausible`
can never auto-ship), verified generated regression test required unconditionally, single-
subsystem scope, no judge hedges, plus new deterministic compensating controls for the removed
per-PR human approval — diff content lint (no CI/workflow/vendored/manifest paths, no added
URLs/blobs), prompt-injection lint on stored AND freshly-refetched issue text, size caps, a
crash-safe 2/day outward cap, adaptive per-repo concurrency from repo health
(`cloud/repo_health.py`: capacity 1+merges capped 3; CHANGES_REQUESTED or recent close-
unmerged forces 0), and a circuit breaker (`cloud/circuit_state.json`) that any auto-shipped
PR closed unmerged trips; re-arm is manual. Gates previously enforced only as operator
procedure (2, 3, 6, 7, 8 of `knowledge/autoship_policy.md`) are all STRENGTHENED by
codification; the trivial test-less exception is removed. Without `OMEGA_AUTOSHIP` the
pipeline behaves exactly as v2.4.0; rollback is `executor.enabled=false` (single switch).
Operator-driven flows (issue selection outside the scout queue, review responses) continue to
require human approval.

v2.5.1 (2026-07-31) — four-tier adversarial audit of the above, no new capability. Gate
behaviour changed in four ways, all tightening: injection lint rebuilt around a gate/redactor
split (a precise gate, since a hit drops the candidate; a greedy redactor, since a hit only
removes text from judge 2's copy) so the two judge passes read materially different text and
"correct twice" stops measuring the same steer twice; judge INFRA failures
(`judge_timeout`/`judge_unparsed`) and a skipped second pass now fail with their own reasons
instead of being recorded as judge disagreement; test-file classification by filename only
(directory placement never implies "test"); and issue text over the size/NFKC-expansion caps
is REJECTED rather than truncated. The executor now exits nonzero for as long as it is halted,
not only on the run that halted it. `PIPELINE_VERSION` is bumped so pre- and post-audit
bundles stay distinguishable in the reject taxonomy — their gate evidence is not comparable.

---

## Current architecture: v2 (agentic localize + fix)

As of 2026-06, Omega is being rebuilt around **agentic sessions** after a
ground-truth evidence chain invalidated the original keyword pipeline. The v2
core is a constrained `claude -p` session (Read/Grep/Glob/Edit/Write, **no
Bash**, issue text only) that explores a real checkout and produces a patch.

### The evidence chain (why v2 exists)

| Stage | Question | Result | Artifact |
|---|---|---|---|
| **1 — Backtest** | Did the old pipeline actually fix things? | No. It never submitted a PR (`create_pr` never called; `pr_url=""`), and keyword localization matched the human fix file **0/7** times. "Local-test-pass" is a benign-ness check, not correctness. | `scripts/backtest_outcomes.py` → `backtest_results.jsonl` |
| **1 — Localization** | Keyword vs agentic file-finding | keyword **top-1 11%** vs agentic **88–93%** (top-3 100%) on 37 ground-truth issues | `scripts/benchmark_localization*.py` |
| **2 — Fix generation** | Do agentic patches match merged human fixes? | **any_exact 80%**, judge **equivalent 49%**, Go ≈ non-Go (no language ceiling) | `scripts/benchmark_fix_agentic.py` → `benchmark_fix_agentic_results.jsonl` |
| **3 — Execution** | Do patches pass the maintainer's own tests? | **4/5 of decisive runs validated**; the comparison judge's `equivalent` verdict = **3/3** execution-validated (calibrated quality proxy) | `scripts/benchmark_validate_tests.py` → `benchmark_validation_results.jsonl` |

Validated patches: `go-ethereum#34692`, `go-ethereum#35018`, `grafana#125774`,
`terraform#38671`.

### v2 components

- **`runner/env_guard.py`** — single source of truth for storage. All heavy work
  (clones, Go/cargo caches, cargo target dirs, rustup) lives on the external
  AgentSSD via the `workspace/` symlink; the boot disk is near-full. Aborts if
  the SSD is not mounted.
- **`agent/agentic_session.py`** — the shared constrained `claude -p` session
  (localize + fix). Imported by both the benchmarks and production, so behavior
  is identical everywhere.
- **`agent/judge.py`** — two roles, kept distinct: `compare_patches` (the
  *calibrated* Stage-2 judge, used offline where the merged human fix exists)
  and `review_patch` (a single-patch reviewer for live issues, which have no
  ground truth — its output gates toward **human review**, never auto-ship).
- **`agent/regression_harness.py`** — `validate_with_ground_truth` (the Stage-3
  differential protocol, offline eval) and `run_package_tests` (production:
  run the affected packages/crates on the patched tree).
- **`runner/pipeline_v2.py`** — the orchestrator: env_guard → checkout HEAD →
  agentic fix → affected tests → review judge → open a ready-for-review PR via
  `create_pr` (gated by `ALLOW_PR_CREATION`, default dry-run; drafts retired
  per user directive 2026-07-28) or queue for human review.

The Stage 1–3 benchmark suite (`scripts/`) is retained as the **permanent
regression harness for the pipeline** — run it after pipeline changes to catch
quality regressions against the 37-issue ground truth.

### Honest limits

- A *live* issue has no merged human fix, so the calibrated comparison judge and
  the full differential protocol cannot run inline — those need ground truth and
  live in the offline eval. Inline production gates are weaker by necessity
  (single-patch reviewer + affected-test run) and shipping is human-gated.
- Execution evidence is thin (5/35 Stage-3 runs were decisive) and the benchmark
  is biased toward small, localized bugs. v2 is validated for that regime;
  widening issue complexity must be incremental and re-measured.

## Legacy (v1) — invalidated

`contribution_loop.py` (multi-domain: GitHub contributions + Gno realms +
crypto Twitter), `realm_loop.py`, `content_loop.py`, `evolution_loop.py`, and
the keyword localizer in `environments/github_env.py:_rank_target_files`. All
pre-2026-06-10 "success rate" numbers (stabilization batches 8–39, "230+ PRs")
measure local-test-pass, **not** fix correctness, and are invalid for quality.
This path is left intact but superseded by `runner/pipeline_v2.py`. (2026-10-21:
the unreferenced v1 loops and web assets were relocated to `legacy/`;
`contribution_loop.py` and its import chain remain at the root because
production `agent/commenting.py` still imports its PR-body helpers.)
