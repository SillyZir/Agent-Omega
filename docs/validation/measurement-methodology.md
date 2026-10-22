# Measurement methodology — the anti-self-deception layer

Omega measures its own competence, which creates an obvious conflict of
interest. This document records the mechanisms that exist to prevent the
system (and its operator) from believing convenient results — and *why*
each exists, usually because a specific failure already happened.

## The founding incident (historical)

The v1 system reported a healthy "success rate" for months. A 2026-06
ground-truth backtest (`scripts/backtest_outcomes.py` →
`benchmark_validate_tests.py`) showed it had never submitted a PR and
matched the humans' fixed file 0/7 times — the metric measured local
test-pass, not fix correctness. Every mechanism below descends from that
lesson: **the measure of success must be external, pre-registered, and
incapable of being quietly redefined after the fact.**

## Mechanisms, with reasons

**Pre-registered acceptance bars.** Every experiment (Rust promotion M1–M6,
testgen S1–S8, presence calibration C1–C4, scout v3 C1–C6) writes its
pass/fail thresholds *before* data collection, in a committed design doc.
Why: thresholds chosen after seeing results are indistinguishable from
rationalization. The bars have real teeth — see the NO-GO records below.

**Temporal train/test separation.** Presence calibration fits on items
≤ 2026-07-31 and evaluates strictly after; scout v3's `ev_model.fit`
*raises* if any training sample is dated inside the shadow window
(`window_start` guard). Why: models scored on data they trained on report
optimism, and with n as small as Omega's (36 terminal outcomes), leakage
would dominate the signal.

**Lookahead protection + frozen artifacts.** Scout v3's feature vocabulary
is frozen by file sha (`feature_freeze_sha`, config-pinned at window
start; scoring refuses on mismatch) and the fitted model artifact is
byte-pinned (`model_sha`); the backtester refuses to overwrite the
production model while the window is open (dev refits go elsewhere). Why:
mid-experiment refits on in-window outcomes were demonstrated *possible*
by the fleet audit (P1-③) — the reflexive "re-run the backtest" would
have silently hot-swapped the model with lookahead.

**Outcome-set single-sourcing.** POSITIVE/NEGATIVE/EXHAUSTION status sets
are defined once (`cloud/scout_v3/ev_model.py`) and re-exported everywhere
they're consumed. Why: a fork of these definitions *actually happened* —
`shipped_auto` postdated registration and was invisible to measurement
until ruled into the sets; repo priors briefly counted a different
positive set than window accounting (fleet P2-e). Divergent definitions
make numbers incomparable without anyone noticing.

**Censored-observation honesty.** A candidate already terminal when v3
first sees it can prove nothing forward, so it is *excluded* from the
window's outcome count — and the exclusion itself is counted
(`censored_first_sight`) so a systematically-late sweep starving the
denominator would be visible. Exhaustion outcomes (`no_fix_exhausted`)
are budget statements, not judgments, and are excluded from both positive
and negative counts. Why: silently counting censored or infra outcomes
manufactures precision.

**One-shot evaluation.** Presence calibration and scout v3 both gate
their final evaluation behind validity minimums (v3: ≥14 sweeps and ≥20
window terminal outcomes; calibration: ≥60 post-cutoff items, ≥10
frozen-eligible, 14 shadow sweeps) and a consumed-marker
(`EVAL_CONSUMED`) that makes the evaluation unrepeatable. A failed eval
burns the window; the next attempt needs fresh data. Why: re-running an
evaluation until it passes is p-hacking with extra steps. The validity
gates are explicitly "not performance criteria and must not be adjusted
based on observed results" (operator ruling, recorded).

**Blinding (pilot).** The human hours-estimate for every pilot task is
recorded *before* any pipeline run touches it — enforced mechanically
(the estimate tool refuses if a pipeline record exists), with a separate
recorded `unblinded` stratum and a 20% stop rule. Why: knowing the
outcome contaminates effort estimates in either direction.

**Frozen denominators / intent-to-treat.** Pilot enrollment is mechanical
(rank order, per-repo cap) and permanent: every enrolled task appears in
the final report with its full cost, including failures at hours_saved
0.0 — the report *refuses to render* if any enrolled row is missing. Why:
post-hoc selection ("the top-20% of tasks") is the standard way service
metrics lie; making the denominator immutable at enrollment removes the
degree of freedom entirely.

**Survivorship controls.** Scout v2's `merge_latency` was found to be
computed only over merged PRs (audit finding, still open in frozen v2 and
regression-encoded as a must-fail-for-v2 test in the v3 suite); v3's
repo acceptance prior counts rejections in its denominator with add-2
shrinkage. Why: scoring repositories by the speed of their successes
recommends exactly the repos that reject most.

**Resampling.** Where a confidence interval is reported (pilot
capacity-savings), it is a bootstrap over enrolled outcomes with 10,000
resamples and a pre-registered seed (20260815). **No permutation tests
exist in the codebase** — this document deliberately claims only the
statistics that are actually implemented.

**Calibrated instruments before conclusions.** The structural effort
proxy was calibrated against held-out merged *human* fixes and frozen
as-fitted; the report refuses to print proxy-derived hours unless the
calibration flag is true and a pre-registered tolerance check (n≥8
pairs, median |log2 ratio| ≤ 1.0, Spearman ρ ≥ 0.4) passes. The
patch-equivalence oracle (M4) went through four validation batteries and
was only accepted for use after achieving zero false verdicts across all
of them — with its one failed gate (G5) recorded as FAIL-as-registered
plus an explicit operator exception, not quietly re-scored.

## Worked example: the H1 experiment — promising, and rejected anyway

`knowledge/h1_counterfactual_testgen_design_2026-08.md` (+ the preserved
revert patch). Hypothesis: adding counterfactual-awareness instructions to
regression-test generation would improve mutation-kill scores. The
experiment was run with byte-level guardrails (17 file hashes required
identical pre/post; fix diffs required identical so only test generation
varied) and eight pre-registered criteria.

Measured: the mean mutation-kill score **improved** — 0.564 → 0.690,
with two clean per-issue wins in exactly the predicted classes. A system
grading its own homework would have shipped this. The registered bars
said otherwise: S1 required ≥ 0.714 (miss by 0.024), S2 required
wins−losses ≥ 3 (actual 1), S3 required the sentinel issue to kill > 0
mutants (0/18), S6 capped test-size growth (+48% over cap). **Reverted.**

The epilogue is the sharper lesson: a follow-up analysis found the
sentinel instrument itself was defective (7 of its 10 frozen mutants sat
in an unrelated JSON encoder, unreachable by any test of the actual
issue). The numbers were re-scored with the defect excluded — and the
corrected mean (0.806 vs a corrected bar of 0.808) *still* missed, with
S2 and S6 still failing. The revert stood on both the pre-registered rule
and the corrected evidence. This is the intended shape of every Omega
experiment: a real measured gain, honestly attributed, and still
insufficient against bars set before anyone knew the answer.

## Issued NO-GO verdicts (the mechanisms have fired)

- **Rust promotion: NO-PROMOTE** (closed 2026-08-14). M1/M2 at 18/18
  ceilings, but M4 decisive-equivalence at 3/18 vs a ≥13 bar — the
  registered rule "decisive < 70% ⇒ the run cannot support promotion
  regardless of pass rate" applied exactly as written. Rust autoship
  stayed closed; the response was to build a better oracle and register
  a second experiment, not to soften the bar.
- **Testgen C1/C2/C3 and H1: reverted** (2026-08-01/02) per registered
  criteria; implementations preserved as patches for auditability.
- **Presence calibration: expected-fail recorded pre-eval.** No fit-pool
  threshold meets C1+C2, and the artifact says so — the eval is expected
  to fail honestly rather than be tuned to pass.
