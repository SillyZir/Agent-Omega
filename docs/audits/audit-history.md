# Audit history

A chronology of the audits Omega has run on itself, what they found, and
what was done. All hashes verified against `git log` at writing time
(2026-09-02). This is a record of real defects in Omega — its existence
is the point: the system's reliability claims rest on adversarial
self-examination, not on the absence of found bugs.

## 2026-06 — the founding invalidation (historical)

A ground-truth backtest proved the v1 system's success metric measured
nothing (zero PRs ever submitted; 0/7 file-localization match against
merged human fixes). v1 was discarded; the agentic v2 architecture was
built and validated stage-by-stage (localization 93% top-1; fix quality
benchmarked against merged human fixes; execution validated against
maintainers' own tests).

## 2026-07 — hardening and honest measurement

- **v2.3.4 audit hardening**: nine bug classes fixed across duplicate
  detection, disclosure compliance, judge infra-handling, false-green
  tests, queue blind spots, and ops counters.
- **Blinded patch-quality audit** (2026-07-31,
  `knowledge/patch_quality_audit_2026-07.md`): 55 scoring agents, 33
  blinded pairs vs merged human fixes. Result recorded without spin:
  parity on the fix, **0/33 on test quality** — the finding that drove
  the testgen requirement and research line.
- **v2.4.0** (`7538dbb`): regression-test generation enabled as a
  validated freeze exception; **v2.5.0** (`95b8d7c`): the controlled
  autoship experiment — every gate code-enforced at the push decision;
  went live 2026-08-04 (`1797370`) after two clean shakedown days.

## 2026-08 — experiment discipline in action

- **Testgen C1/C2/C3** measured and **reverted** per pre-registered rule
  (`49ed354`, 2026-08-01); **H1 counterfactual testgen** measured
  (+0.126 mean gain), failed registered bars S1/S2/S3/S6, **reverted**
  (`a9c8e3e`, 2026-08-02); the follow-up sentinel-defect analysis
  corrected the numbers and upheld the revert.
- **Scout v2 adversarial audit** (2026-08-06): 10 findings; mechanism
  batch fixed with the acceptance gate re-run (not re-pinned); scoring
  surface frozen under sha pin; semantic batch deliberately deferred and
  later landed as one designed change (batch B, 2026-08-13).
- **Rust promotion experiment**: Phase X completed 18/18; **NO-PROMOTE**
  verdict recorded exactly as registered (`363e5b7`; report closed
  2026-08-14) — M4 decisive-equivalence 3/18 vs ≥13. Response: build a
  better oracle (v2 → v3.1, four validation batteries, zero false
  verdicts; G5 FAIL-as-registered with explicit operator exception,
  `d2df0b1`) and register Experiment 2, which is accruing its corpus.
- **Presence Phase B/C audit** (2026-08-26): four parallel reviewers,
  independently re-verified findings; remediation design-gated.
- **no_fix reclassification** (2026-08-08): all four historical `no_fix`
  records proved to be budget exhaustion, not judgment — the
  `no_fix_exhausted` retryable class was created and legacy records
  corrected at read time; extended 2026-08-28 by the
  non-conclusion-vs-judgement ruling (tidb #70636).

## 2026-09-01/02 — the system-level audits

**Operator-driven system audit** (read-only, full lifecycle): 3 P1, 4 P2,
4 P3 findings. Fixed same-day: queue single-writer lock + merge-on-write,
push-divergence visibility + autostash, chain-run guards, v3 spend
accounting, freeze-pin chained (`f3dabff`).

**38-agent independent fleet audit** (six lenses: outward core, queue
concurrency, v3 isolation/measurement, feedback capture, chain
crash-safety, cross-cutting races; every finding adversarially verified
by a separate refute-by-default agent):

- **32 findings → 31 verified real, 1 refuted** (the refutation earned
  by an empirical multi-process stress test).
- **Six P1 root causes** — executor timeout defeating the
  no-record-halt; gate-config fail-open (`cfg={}` swallow at the
  enforcement point); backtest lookahead/model hot-swap; monitor
  state-before-summary event loss; the preship/executor
  `repo_outcomes` race (hidden behind an import indirection); unguarded
  manual pipeline runs sharing checkouts. **All fixed** in `c925759`
  with regression tests, including two operator-directed edits to the
  frozen pipeline file.
- **Twelve P2 findings** — recovered-outward continuation, `--state-dir`
  without dry-run, chain-marker overwrite, pid-0 liveness, forked
  outcome sets, censored-observation visibility, zero-observation
  sweeps, feedback oldest-page pagination, autostash stranding, masked
  maintainer comments, untimed chain git calls, dishonest green banner,
  report truncation on re-run, same-day operator-ship capacity
  blindness. **All fixed** in `c916542`.
- The fleet also **overturned two of the operator-side audit's own
  verdicts** (a refuted finding proved real via an indirection the
  original grep missed; a P2 upgraded to P1 on reachability evidence) —
  recorded here because it demonstrates why the independent layer
  exists.
- **Post-remediation validation**: full battery green (≈13 suites,
  including the frozen-pipeline replay suite proving behavior
  preservation); the first full production chain over the remediations
  ran clean on 2026-09-02 (exit 0, all guards live, one candidate
  correctly gate-held, seven correctly policy-skipped).

## 2026-09-02 — patch-quality audit v2

Pre-registered re-measurement of the July test-quality finding on the
post-testgen cohort (every PR shipped ≥ 2026-08-02, n=14, scripted
nearest-neighbor human comparators, diffs-only blinding, 27 agents).
Result: **partial movement** per the registered rules — test-quality
median 1 → 3.5, ceiling broken, failure modes reduced to absence-only —
but presence 57% vs the 80% bar (operator-lane ships carry no testgen
gate), and the blinding probe failed at 93% identification (stylistic
tells, dominantly stale PR bodies on reworked PRs), downgrading all
comparative claims to descriptive as pre-committed. Report:
`knowledge/patch_quality_audit_v2_2026-09.md`.

## Standing audit posture

Regression suites (`scripts/test_*.py`, `presence/test_presence.py`,
`commercial/test_commercial.py`) encode every audit finding as a
must-pass check — the suites are the audit trail's enforcement arm.
Remaining known-open items are tracked, not hidden: nine P3 hygiene
findings from the fleet audit, four deferred-by-design v2 scoring
semantics (regression-encoded against v3), and one reproducible
instrument failure (caddy #7721 judge_unparsed) awaiting a design-first
fix.
