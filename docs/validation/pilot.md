# The commercial pilot — a frozen measurement cohort

`commercial/` — a measurement instrument answering one question: **does
Omega's verified-fix work have demonstrable economic value against
blinded human estimates?** It is passive by standing operator directive:
no interim interpretation, daily integrity checks, stop-and-surface on
any violation. This document describes mechanics only and deliberately
reports **no results**.

## Cohort: pilot_2026-08 — COMPLETE at 30/30 (2026-09-02)

- **Enrollment is mechanical and permanent.** Tasks enter one at a time
  (`enroll.py`), selected in scout rank order under a per-repo cap (8,
  Amendment A4), with a human-judged task type assigned at enrollment,
  blind to outcome. `enrolled_ts` is the anti-cherry-pick anchor: once
  enrolled, a row is reported *whatever happens to it* — infra failures,
  aborts, operator rejections and no-fixes all stay in the denominator
  with hours_saved = 0.0 and their full cost.
- **Already-run tasks cannot enroll.** Any candidate with an existing
  pipeline record is refused (its outcome is knowable, so a blinded
  estimate is impossible). This rule was exercised for real: an
  operator-requested enrollment of an already-run task was refused and
  the mechanical next-in-rank candidate took the final slot instead.
- **The cohort is full**: 10 seed + 20 daily enrollments; the enrollment
  phase is closed and the preview permanently empty.

## Blinding

The pre-registered estimator is the operator. For every enrolled task, an
hours estimate is recorded **before any pipeline run touches it**
(`estimate.py`) — enforced mechanically: the tool refuses to write a
blinded estimate for an issue that already has a pipeline record. An
explicit `--unblinded` escape exists, is recorded on the row, reports as
a separate stratum, and carries a 20% stop rule. Status at cohort close:
every enrolled row estimated; blind open for none; one unblinded stratum
row (≈4%, under the stop rule).

A **structural proxy** (size-band → hours table) exists as a mechanical
cross-check computed at report time from the patch itself. It was
calibrated against held-out merged *human* fixes (never the pilot
cohort), frozen as-fitted (`proxy_table_v1.json`, calibrated
2026-08-15), and never feeds hours_saved. GitHub calendar time is
deliberately not used — it is not engineering time.

## Daily integrity (`pilot_integrity.py`)

Runs every day under a standing directive. Exit 2 (stop-and-surface
immediately) on: a blinded estimate recorded after its task's first run;
unblinded share > 20%; any preregistration breach (over-enrollment,
per-repo cap, task type outside the frozen enum, enrollment after an
outcome existed); or corrupted/unparsable ledger data. Otherwise it
reports enrollment count, dispositions, blinding status, and (until the
cohort filled) the enrollable preview — facts only.

## Reporting (`pilot_report.py`) — three refusals

The final report is fail-closed against the standard ways service
metrics lie:

1. **Intent-to-treat**: if any enrolled row is missing from the output,
   the report refuses to render at all. Every enrolled task is reported
   or none are.
2. **Unverified rate card**: no dollar figure prints unless the rate
   card is marked verified.
3. **Uncalibrated proxy**: no proxy-derived hours print unless the
   calibration flag is true *and* a pre-registered tolerance check
   passes (n ≥ 8 estimate/proxy pairs, median |log2 ratio| ≤ 1.0,
   Spearman ρ ≥ 0.4).

Where a confidence interval is reported, it is a bootstrap over enrolled
outcomes (10,000 resamples, pre-registered seed 20260815). Metrics are
joined at read time from the production ledgers (pipeline records, queue
statuses, monitor state) — nothing is copied where it could drift.

## What happens now

Nothing but accumulation. Tasks resolve through the normal production
machinery at their own pace; the integrity check runs daily; the report
runs at the preregistered endpoint and not before. `outcome_ts` is t0 of
observation, not the outcome itself. Per the standing passive-mode
directive, no interim numbers are computed, quoted, or interpreted —
including in this documentation.
