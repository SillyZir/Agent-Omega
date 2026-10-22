# Maintainer feedback system

`cloud/feedback_capture.py` + `cloud/feedback_measure.py` +
`cloud/feedback/` ledgers. Operator-approved 2026-09-01 (Phase 1).

## Scope — read this first

The current phase is **capture-and-measure only**:

```
maintainer feedback → persistent evidence → classification → measurement
```

It is explicitly **not**:

```
maintainer feedback → automatic behavioral change
```

No production module reads the feedback ledgers (pinned by
`scripts/test_feedback_ledger.py`); the patch-generation prompt is pinned
feedback-free; and the schema's `promoted` row type — the only path by
which a lesson could become a global rule — **has no writer in the
codebase**. Promotion is operator-only by structural absence, not by
convention. Phase 2 (repo-scoped retrieval of relevant lessons as
*context* for future patches) is designed at the sketch level and
requires a separate explicit operator approval, partly because it would
touch a guardrail-frozen file.

Motivation (from the audit that preceded the build): the machinery
captured review *events* but discarded review *content* — e.g. a k3s
maintainer's standing rule ("any change to install.sh must also update
install.sh.sha256sum") was applied to one PR and persisted nowhere.

## Capture

- **Source**: the monitor's tracked-PR set. An incremental cursor per PR
  (review count, comment count, **and `updatedAt`** — counts alone missed
  file-anchored review comments, a measured blind spot) decides what to
  fetch; steady state is 0–3 PRs/day.
- **Endpoints**: PR reviews, file-anchored review comments, and issue
  comments — paginated up to 4 pages each (a single page returned the
  *oldest* 50 and permanently lost new feedback on busy PRs); a
  still-full fourth page is counted and reported as truncation, never
  silent.
- **Substantive filter**: maintainer-association authors only
  (OWNER/MEMBER/COLLABORATOR), bots excluded (including the `-bot`
  suffix class the upstream helper misses), our own account excluded,
  empty bodies excluded.
- **Idempotence**: row id = `sha256(repo|pr|source|source_id)[:16]`;
  re-ingestion skips existing ids. A concurrency lock (stale-safe,
  dead-pid breaking) serializes runs.
- **Failure honesty**: a failed fetch leaves that PR's cursor unchanged
  (retried next run); budget exhaustion stops gracefully with the spend
  recorded; `historical-baseline` rows are tagged distinctly from `live`
  capture and never mixed silently.
- **Provenance per row**: repo, PR, author + association, GitHub
  timestamp, source kind, review state, file path where anchored,
  verbatim body + body sha, capture mode.

## Classification

A **separate appended row** referencing the capture id — capture rows are
never rewritten (append-only integrity is test-pinned: post-classification
ledger bytes must extend, byte-for-byte, the pre-classification bytes).

- Classes (operator-approved taxonomy): `convention` (a standing repo
  rule), `correction` (specific technical direction), `approach-rejection`
  (premise declined), `preference` (non-blocking taste).
- Scope: `repo` (default) or `global-candidate` — the latter is a **mark
  with zero mechanical effect**, visible to the operator as a promotion
  candidate and nothing more.
- `classified_by: agent | operator`, with operator rows taking precedence
  per id; a recorded `why` is mandatory; structured `hints`
  (paths/keywords) make conventions mechanically matchable later.
- Acknowledgment-only rows (LGTM/thanks) are deliberately left
  unclassified — the recorded convention is "unclassified = no lesson",
  keeping the actionable counts honest.

## Assessment (reuse outcomes)

`--assess` records a judgement row on a *classified* lesson:
`applied | missed | not-relevant`, with a recorded why and the later PR
reference. The missed-reuse rate is computed **only** over assessed
applied+missed outcomes; unassessed relevance reports as UNASSESSED,
never as zero and never as success.

## Measurement display safety

Ledger bodies are third-party text. The measurement report runs the same
injection lint the ship path uses and **withholds flagged bodies** from
display (`[INJECTION-FLAGGED — body withheld; see ledger]`); a lint error
also withholds. The verbatim evidence stays in the ledger; only the
report surface is sanitized.

## Current state (2026-09-02 — demonstrated, will drift)

35 captured rows across 3 repos (caddy 27, rclone 7, k3s 1); 26
classified (21 actionable: 5 conventions, 16 corrections; 2
approach-rejections; 3 preferences); 3 marked global-candidate; **1
assessment recorded** — the caddy "no partial race fixes" lesson,
captured from a maintainer review on 2026-08-28, classified 2026-09-01,
and demonstrably **applied** in the PR 7961 rework on 2026-09-02: the
ledger's first ARMED→APPLIED case. The k3s sha256sum convention is armed
with path hints awaiting its first relevant PR.
