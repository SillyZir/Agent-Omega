# Autonomy and authority model

Who — or what — may cause an outward action, under which conditions, as of
2026-09-02. This is the operator-governance layer the README summarizes as
"reasoning does not confer authority."

## 1. The three action classes

| Class | Meaning | Who acts | Examples (real) |
|---|---|---|---|
| **Autonomous** | The executor may claim a candidate, run the pipeline, and ship a PR with no human in the loop — if and only if every gate passes | control plane | DeepSeek-Reasonix PR 9707 (first and so far only fully autonomous ship) |
| **Operator-gated** | Omega discovers, investigates, prepares patches and private briefs; a human reviews to comprehension, pushes, and writes all outward prose | operator, from agent work | every caddy/k3s/go-task ship; the caddy 7961 rework |
| **Maintainer-dependent** | Outcomes Omega cannot cause at all: merges, approvals, label changes that unlock cli/cli candidates | upstream humans | all 9 merges; brandond's approval on k3s 14552 |

Presence (non-code posts) is stricter than all three: **every** outward
post requires a per-item operator approval that binds the exact text by
sha; the subsystem's config wall (`zero_autonomous`) is enforced at config
load, in the datastore (G5), and by the posted-event append boundary.

## 2. The fifteen ship gates

`agent/autoship_gates.py::GATE_NAMES`, evaluated by
`runner/pipeline_v2.py::_autoship_check` at the push decision. Every gate
runs on every evaluation; any exception inside a gate fails it closed; a
config that cannot be read fails the *whole check* closed (the inner
swallow that once made policy gates pass vacuously was removed — audit
P1-②).

| Gate | Condition |
|---|---|
| `breaker_armed` | circuit breaker file present and armed |
| `daily_cap` | outward intents today < cap (2), own in-flight intent excluded |
| `concentration` | per-repo capacity remaining ≥ 1 (see §4) |
| `repo_autoship_allowed` | per-repo policy permits unattended shipping; malformed policy denies |
| `repo_required_labels` | live GitHub labels satisfy the repo's declared label policy; declared-but-unverifiable denies |
| `scope_single_subsystem` | changed files confined to one subsystem |
| `judge_correct_twice` | both judge passes returned `correct`; infra verdicts and skipped second pass fail with their own reasons |
| `tests_pass` | affected package tests passed |
| `validated_languages` | every changed source file is in a validated language *and* the language actually executed |
| `testgen_generated` | regression-test generation succeeded (no test-less exception) |
| `no_material_caveats` | declaration/review text free of hedging that signals unresolved risk |
| `content_lint` | diff hygiene (hunk-aware; spoofed headers hard-reject) |
| `injection_lint` | issue text passes the injection heuristics (see §5) |
| `size_caps` | diff and file-count bounds |
| `changelog_lint` | repo-required changelog artifacts present and well-formed |

Gate failures are recorded verbatim on the candidate
(`queued_for_review` + `gate_failures`) — the operator sees exactly which
gates failed and why.

## 3. Per-repository policy

`cloud/scout_config.json → executor.repo_policy`, single-sourced through
`cloud/repo_health.py` helpers so the executor, the gates, and scout v3
can never diverge on interpretation:

- `autoship: false` — six repos (cilium, caddy, authelia, k3s, go-task,
  hister), each with the admission reasoning in its `note` and a fuller
  audit in `knowledge/repos/`. These encode *maintainer-imposed* rules
  (e.g. caddy's "respond in your own words, not through an AI") and one
  operator security ruling (authelia); they are not tunable by the agent.
- `autoship: true` + `required_labels`/`excluded_labels` — cli/cli
  (`help wanted` required, `core` excluded, per its CONTRIBUTING). The
  label state is verified against **live** GitHub twice: an executor
  early-skip (saves the pipeline attempt; candidate stays pending and
  re-checks daily) and the `repo_required_labels` gate at ship time (the
  enforcement point). The "explicit acceptance criteria" alternative in
  their docs is deliberately *not* machine-approximated — autonomous
  action takes the conservative subset.
- Absent entry — autoship permitted, all global rules still apply.
- Interaction tiers (`knowledge/interaction_policy.md`): (a)
  human-interaction-required, (b) disclosure-required, (c) no written AI
  policy. Disclosure is **mandate-only** (operator ruling 2026-09-01): a
  disclosure section is inserted exactly when the repo's own policy
  requires one, using the recorded template
  (`contribution_reqs.AI_DISCLOSURE_BODY` in the operator's directed
  wording) — never free-composed. A repo that *prohibits* a production
  mode is handled by comply-or-skip, never disclose-and-do-it-anyway.

## 4. Capacity and budgets

`cloud/repo_health.py`: default limit 2 open PRs per repo; degraded to 1
on CHANGES_REQUESTED, an open unmerged autoship PR, a breaker trip for
that repo, or all-stale-unreviewed PRs; capacity 0 for 14 days after a
close-unmerged; absolute ceiling 3 (third slot operator-only, via
`scripts/preship_check.py --justify` with a recorded justification). The
same tool is the operator lane's testgen visibility point (F2,
2026-09-02): a ship run requires `--issue N`, and a candidate whose
pipeline record lacks a `generated` regression test blocks unless the
operator records a `--test-justify` reason — absence is a logged
decision, not a silent default (on autoship it remains a hard gate). Open
PRs are counted from three sources — live monitor state, the outcomes
ledger (same-day autoships), and `ops_tracked_prs.json` (same-day
operator ships) — so no ship path is invisible to concurrency. Daily
budgets: 2 outward ships, 3 pipeline attempts, both derived from the
write-ahead intent ledger, never from in-memory counters.

## 5. Untrusted input handling

Issue text, maintainer comments, and repo documents are third-party
input. Defenses are layered and deliberately *not* prompt-dependent: the
injection lint (NFKC-normalized, homoglyph-folded, comment-stripped,
bounded-quantifier heuristics) runs over stored and freshly-fetched text
on the autoship path and as a gate everywhere; the second judge sees
neutralized text so a steer produces divergence; and the gates that
matter most (tests, testgen, scope, size, content) do not read prose at
all. Documented residual: the paraphrase space is unbounded — the lint is
a tripwire, not the wall; the wall is the non-prose gates. Feedback and
presence surfaces lint third-party text at display time and withhold
flagged bodies from reports.

## 6. Fail-closed doctrine and the uncertain-outward state

Recurring invariants, enforced in code and pinned by tests:

- **UNKNOWN ≠ PASS**: unreadable config, failed fetch, unparsable state,
  unknown outcome strings — all deny/halt.
- **Outward-or-uncertain ⇒ operator**: `push_failed`,
  `pr_create_failed`, `ship_error_postpush`, pipeline timeout, missing
  pipeline record, and *recovered* instances of any of these at startup
  reconciliation all set `blocked_reason`. A set `blocked_reason` halts
  every subsequent day (exit 1, visibly red) until the operator clears it
  after verifying the live fork/PR state.
- **Infra failure ≠ conclusion**: budget exhaustion (`no_fix_exhausted`),
  judge infra verdicts, and environment errors are retryable or
  operator-routed, never recorded as judgments about the issue.
- **New policy ⇒ operator**: when the system meets a case its rules don't
  cover, the item goes to an exception queue with the exact blocking
  reason; the agent does not invent policy (standing autonomy
  authorization, `knowledge/autonomy_authorization_2026-08-31.md`).

## 7. Authority state model (candidate statuses)

```
pending ──▶ in_pipeline ──▶ shipped_auto ─────────────▶ (monitor tracks)
   │            │      └──▶ queued_for_review ──▶ operator: ship/reject/rework
   │            └── crash → startup reconciliation → recovered outcome │
   ├──▶ policy_labels / no_capacity / stale_* (re-checked or expired)  │
   ├──▶ needs_operator (operator-only repo, injection flag, text drift)│
   └──▶ terminal: rejected_judge / rejected_operator / no_fix /        │
        insufficient / shipped_operator ◀──────────────────────────────┘
```
`timeout_blocked` and every outward-uncertain condition freeze the whole
system (not just the candidate) behind `blocked_reason`.
