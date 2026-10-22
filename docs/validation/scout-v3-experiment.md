# Discovery: scout v2 (production) and the v3 shadow experiment

## Scout v2 — the production discovery layer

Two stages, both deterministic (`cloud/scout_repos.py`,
`cloud/scout_issues.py`, orchestrated by `cloud/scout_run.py`):

- **Repository scout** searches the ecosystem, scores candidate repos
  (maintainer responsiveness over recent external PRs, merge ratio,
  activity, backlog, CI health, named subtractive burdens), and
  **proposes** — admission always requires the operator to edit
  `scout_config.json`, preceded by a per-repo interaction-policy audit
  recorded in `knowledge/repos/`.
- **Issue scout** hard-filters (assigned, locked, excluded/stale labels,
  too-fresh, maintainer-dismissed, already-seen), scores additive
  evidence signals (maintainer-confirmed, repro evidence, bug label,
  single-subsystem scope, recency, discussion health, repo quality),
  verifies top candidates through the duplicate checker, and queues at
  most 3/day.

**The freeze.** v2's scoring surface is frozen under a sha pin
(`cloud/scout_freeze.json`, verified daily in the chain, non-gating but
RED-visible on drift). An adversarial audit (2026-08-06,
`knowledge/audit_scout_v2_2026-08.md`) produced 10 findings; the
mechanism batch was fixed with the pre-registered acceptance gate
*re-run, not re-pinned*; four semantic findings (survivorship-biased
merge latency, near-tautological freshness, bots counted as maintainers,
burden mention-vs-requirement) were deliberately deferred — re-pinning a
frozen gate whenever it complains is how it stops being a gate. Those
four are regression-encoded in the v3 test suite as behaviors v3 must
get right.

**Measured v2 performance** (2026-07-30 → 2026-08-31, from the v3 design
audit `knowledge/design_scout_v3.md`): 75 candidates queued; 57% went to
operator-gated repos (routing that buys zero autonomous work); terminal
precision 8/36 ≈ 22%; wasted-investigation ≈ 35%; confidence scores did
not discriminate outcomes (the 0.9–1.0 band held more rejections than
ships). These numbers are v3's baseline, not a criticism hidden in a
drawer.

## Scout v3 — shadow experiment (live, uninterpreted)

`cloud/scout_v3/` — a policy-first, outcome-calibrated redesign running
**shadow-only**: it writes exclusively under its own tree (structurally
guarded write path), the executor never reads its output, and nothing it
computes affects production routing.

**Design** (`knowledge/design_scout_v3.md` + `scout_v3_phase_a_plan.md`):

1. **Policy gate before any scoring** ("validity before value"): denied
   and dual-use repos refused at source; per-repo autoship routing;
   live-label policy (advisory at rank time — act-time enforcement stays
   with the executor and ship gate); burden extraction distinguishing
   *required/forbidden/mentioned* with external-stub detection; every
   drop counted in a visible ledger, never silent.
2. **EV model**: `EV = P_fix × P_gates × P_accept × V_channel − C_invest`,
   fit as coarse monotone buckets (a cell with < 5 outcomes is not
   split — with 36 terminal outcomes and 8 positives, anything finer is
   fiction, and the code says so).
3. **Budgeted channels**: autonomous slots derived from *open* autoship
   capacity (a saturated repo contributes zero); the operator channel is
   explicitly priced and cannot be spilled into (its price, R1, remains
   unratified — provisional-stamped in every output).

**The frozen evaluation.** The shadow window opened 2026-08-31 with the
feature vocabulary sha-frozen (`e88fcf49…`) and the pre-window model
artifact byte-pinned (`ee5ea012…`); the shadow runner refuses to score on
any mismatch, and the backtester refuses to touch the production model
while the window is open. Sweeps are chained daily (non-gating, 900s
watchdog), idempotent per day with pre-spend duplicate refusal, and
kill-safe. Outcome accounting is strict: only candidates *non-terminal at
first sight* that later reach a terminal status count toward the window
(`window_terminal_outcomes`); already-terminal-at-first-sight candidates
are excluded **and counted** (`censored_first_sight`), so late sweeps
can't silently starve the denominator.

**Promotion criteria** (pre-registered in the design; a one-shot
evaluation self-refuses below the validity gate of ≥14 sweeps and ≥20
window outcomes, and forever after an `EVAL_CONSUMED` marker exists):
≥2× v2's precision at equal-or-smaller volume with an overfit tripwire;
wasted-investigation ≤ 20%; policy-invalid queue share ≤ 5%; zero
regression on denials/injection/holds; and at least one v3-only
discovery reaching a positive terminal outcome. **More candidates is
explicitly not a criterion.** Promotion, if it ever happens, is a single
reversible config flip with v2 kept armed; v2 is never deleted during
the experiment.

**Current state (2026-09-02, factual only):** 3 of 14 creditable sweeps;
0 of 20 window terminal outcomes; censored count 2; divergence reports
accumulating daily. Backtest context (not the promotion eval): simulated
precision 0.259 vs v2's 0.222 with four of eleven evidence features dark
offline, and an internal temporal sanity split showing top-half precision
0.667 vs 0.308 base. **No conclusion is drawn from any of this, and none
will be until the registered one-shot evaluation runs.** Interpreting
shadow results early is precisely the failure mode the frozen procedure
exists to prevent: intermediate numbers select for noise, and a system
allowed to peek will find reasons to promote itself.
