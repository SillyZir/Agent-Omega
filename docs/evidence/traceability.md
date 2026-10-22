# Auditability and the evidence chain

The objective is **reconstructability**: for any outward action, a
reviewer should be able to determine — from files in this repository —
why it occurred, what evidence supported it, and what happened next. This
document maps the chain and then walks it for a real PR.

## The chain and where each link is recorded

```
issue ─▶ discovery ─▶ candidate ─▶ pre-flight ─▶ pipeline ─▶ patch
  │          │            │            │             │          │
  │   scout report   scout_queue   dispositions   pipeline    bundle record
  │   (committed     .jsonl row    in executor    log file    in pipeline_v2
  │   daily)         (score break- report         per run     _queue.jsonl
  │                  down + "why") (committed)                (nonce-keyed)
  ▼
tests ─▶ judge ─▶ gates ─▶ ship decision ─▶ outward action ─▶ maintainer ─▶ outcome
  │        │        │           │                │           response
  bundle  verdicts  per-gate   intent row     PR URL in      feedback     monitor
  .test   ×2 with   ok/reason  (write-ahead,  bundle.ship;   ledger rows  reconcile;
  field   reasons   list in    repo_outcomes) tracking       (verbatim,   durability
                    record                    files          classified)  (d3)
```

Key properties per link:

- **Discovery**: every queued candidate carries its full scoring
  breakdown and a human-readable `why` list; every *rejected* repo or
  issue appears in the committed daily scout report (rejection
  histograms; denied-repo surfacing). Scout v3 additionally counts every
  policy drop in a visible ledger — the system does not have silent
  discards.
- **Candidate → pipeline**: the executor stamps each invocation with a
  per-run nonce (`OMEGA_RUN_ID`); the pipeline writes exactly one terminal
  bundle record carrying that nonce, which is how startup reconciliation
  can prove what a crashed run did or didn't do. Every considered
  candidate gets a disposition row in the committed daily executor
  report — including the ones nothing happened to, with the reason.
- **Verification**: the bundle records test results, both judge verdicts
  *with their reasons*, the test-generation outcome, and — on the
  autoship path — the complete per-gate `checked` list (every gate, ok or
  not, with its reason string), not just failures.
- **Outward action**: a write-ahead intent precedes any push-capable
  subprocess; the ship result records PR URL, branch, and artifacts;
  tracking files and the outcomes ledger are updated in the same flow,
  with a hard error if the two tracking files desynchronize.
- **Afterlife**: the monitor's committed daily summaries record state
  transitions and review events; maintainer feedback text is captured
  verbatim (sha-hashed, provenance-stamped) into per-repo append-only
  ledgers; merges/reverts feed the durability tracker.

## Integrity mechanisms

- **Append-only ledgers with validated appends**: the presence event
  ledger enforces a state machine at the append boundary (illegal
  transitions are refused, discards cannot silently resurrect —
  reopening requires a recorded operator authorization in the event
  payload); feedback ledgers never rewrite (classification and
  assessment are separate rows; tests pin byte-prefix preservation).
- **Hashes as commitments**: approved presence text is sha-bound at
  approval (posted must match verbatim); scout v2's scoring surface,
  v3's feature vocabulary and model artifact, the Rust experiment's
  adapter files, and the H1 experiment's 17 guarded files are all
  sha-pinned, so "nothing changed" is checkable, not asserted.
- **Cursors that fail toward re-work**: capture cursors advance only
  after successful fetch+ingest; idempotent ingestion makes refetch
  harmless — the failure direction is duplicate effort, never lost
  evidence.
- **Independent integrity checkers**: `presence/presence_integrity.py`
  re-validates the entire event ledger from scratch daily (a hand-edited
  ledger fails independently of the append guard);
  `commercial/pilot_integrity.py` re-derives blinding/preregistration
  compliance daily; the chain verifies the v2 freeze pin daily.
- **Derive-from-state doctrine**: the operator's daily digest and
  standing queue are derived from authoritative files at read time —
  never from memory or stale indexes (a near-duplicate ship caused by a
  stale index line is the recorded incident behind this rule).

## Worked reconstruction: DeepSeek-Reasonix PR 9707 (the first autoship)

Every step below is answerable from committed or ledgered artifacts:

1. **Why was this repo eligible?** `knowledge/repos/esengine__DeepSeek-Reasonix.md`
   records the admission audit and the operator's explicit
   admit-with-autoship ruling (2026-08-31), including the
   no-CONTRIBUTING-found caveat and its enforcement path (live
   repo-profile re-scan each ship).
2. **Why this issue?** The `scout_queue.jsonl` row for issue #9516
   (day 2026-09-01) holds confidence 0.87 with the full signal
   breakdown; the committed scout report for that day shows its ranking
   context.
3. **Why did it run?** The executor report for 2026-09-01 shows the
   candidate dispositions for the whole day — including the three
   cli/cli candidates skipped by live label policy that morning — and
   the intent row in `repo_outcomes.json` timestamps the claim.
4. **Why did it ship?** The pipeline bundle (nonce `exec-2026-09-01-…`)
   records the declared fix summary, passing package tests, generated
   regression test, two `correct` judge verdicts with reasons, and all
   fifteen gates passing with their reason strings.
5. **What exactly went out?** The ship record holds the PR URL and
   branch; the PR body carries no AI disclosure — which the disclosure
   policy record shows is *compliant* (mandate-only ruling; this repo
   mandates none).
6. **What happened next?** Monitor summaries from 2026-09-02 onward
   track its state; any maintainer response will land in
   `cloud/feedback/esengine__DeepSeek-Reasonix.jsonl` verbatim; a merge
   or close updates capacity, breaker state, and the durability tracker.

The same walk works for operator-gated work with one addition: the
interaction log (e.g. the cd-8 entry in
`knowledge/repos/caddyserver__caddy.md` for the 7961 rework) records who
authored what — agent investigation and patch, operator comprehension
review, operator-composed reply — because attribution is part of the
evidence model, not a courtesy.
