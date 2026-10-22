# Production record — demonstrated external behavior

The factual record of Omega's outward actions as of **2026-09-02**,
sourced from `cloud/repo_outcomes.json`, `cloud/summaries/2026-09-02.md`,
`ops_tracked_prs.json`, and the presence ledger. This file reports what
happened; it makes no performance claims.

## Rungs of evidence — read the table with these

1. **Local tests pass** — weakest: Omega's own harness liked the patch.
2. **Internal validation** — judges, gates, blinded internal audits.
3. **PR created** — the work entered a real repository's process.
4. **Autonomous ship** — the PR was created with no human in the loop.
5. **Maintainer review** — an unaffiliated human engaged with the work.
6. **Maintainer approval** — that human endorsed it.
7. **Merged upstream** — the change entered the project.

A merged PR demonstrates a successful contribution through that
repository's real review process — under that repo's standards, that
maintainer's attention, on that class of issue. It does **not**
demonstrate universal autonomous software-engineering reliability, and
nine merges across four repositories is a small sample by any standard.

## Pull requests (21 tracked)

| PR | State (2026-09-02) | Shipped by | Notes |
|---|---|---|---|
| caddyserver/caddy #7898 | **MERGED** | operator | first merged PR (2026-07-30) |
| caddyserver/caddy #7904 | **MERGED** | operator | |
| caddyserver/caddy #7905 | **MERGED** | operator | |
| caddyserver/caddy #7916 | **MERGED** | operator | reworked to the maintainer's direction before merge |
| caddyserver/caddy #7920 | **MERGED** | operator | carried the maintainer's own-words interaction direction (cd-8) |
| rclone/rclone #9639 | **MERGED** | operator | |
| rclone/rclone #9726 | **MERGED** | operator | |
| rclone/rclone #9847 | **MERGED** | operator | merged 2026-09-01 |
| ethereum/go-ethereum #35433 | **MERGED** | operator | |
| rclone/rclone #9642 | CLOSED unmerged | operator | withdrawn by us: the approach (NFC fold) was lossy; design question posed to the maintainer instead |
| caddyserver/caddy #7919 | CLOSED unmerged | operator | maintainer declined the premise; withdrawn rather than iterated |
| esengine/DeepSeek-Reasonix #9707 | OPEN | **autoship** | the first and so far only fully autonomous ship (2026-09-01); CI green; under review |
| k3s-io/k3s #14552 | OPEN | operator | one maintainer approved after a requested fix; a second's changes-requested pending re-review |
| caddyserver/caddy #7961 | OPEN | operator | reworked 2026-09-02 to the maintainer's immutable-options direction |
| go-task/task #2998, #2999 | OPEN | operator | |
| rclone/rclone #9851 | OPEN | operator | operator-directed rework ship (2026-09-02); the destructive half of the original patch was withheld and posed as a design question |
| go-git/go-git #2238 | OPEN | operator | long-idle; first-contributor CI gate |
| encoredev/encore #2536 | OPEN | operator | |
| danielgtaylor/huma #1101 | OPEN | operator | |
| helm/helm #32553 | OPEN | operator | |

Summary: **9 merged / 2 closed-unmerged / 10 open**; 20 operator-shipped,
**1 autoship**. Merges span caddy (5), rclone (3), go-ethereum (1) — all
through those projects' ordinary review, several after
changes-requested cycles that Omega-prepared and operator-shipped
rework resolved.

Honest annotations the table can't carry: the two closed PRs are part of
the record on purpose — one withdrawn by us on recognizing a lossy
design, one closed on a declined premise; both produced recorded policy
lessons. The autoship PR proves the fifteen-gate path executes end to
end in production; its *outcome* is still the maintainers'.

## Non-code contributions (presence)

| Item | Venue | Status |
|---|---|---|
| fiber #2837 analysis (L1) + follow-up (L3) | gofiber/fiber issue | posted; maintainer replied (v3-only fix confirmed) — reply captured by the watcher, operator chose not to respond |
| etcd #22012 analysis (L2) | etcd-io/etcd discussion | posted 2026-09-02 with per-item operator approval; amended twice the same day under operator rulings (disclosure format; citation-tag removal) |
| go-task #2862 writeup (L4) | — | approved by the operator; not yet published |

Every posted item was individually operator-approved with the exact text
bound at approval; the amendments are recorded deltas, not silent edits.

## Internal validation results that bound the claims above

- **Blinded patch-quality audit** (2026-07-31, 33 paired comparisons):
  parity with merged human fixes on correctness/readability; better on
  minimality; **0/33 pairs favored Omega on test quality**. That deficit
  is why regression-test generation became an autoship *requirement* and
  a standing research line.
- **Patch-quality audit v2** (2026-09-02, pre-registered, 14 pairs on
  the post-testgen cohort): test quality showed **partial movement** —
  median 1 → 3.5, the July ceiling of 3 broke (six 4s, one 5), and of
  the tests present, 7/8 were judged to fail without the production
  change (July's tautological/theatre modes gone). But test presence was
  8/14 = 57% vs the registered 80% bar — every test-less PR shipped via
  the operator-review lane, where testgen is not a ship gate — and the
  run's blinding failed (13/14 machine-arm identification, chiefly via
  stale PR bodies on reworked PRs), so all agent-vs-human comparisons in
  it are descriptive. Full report:
  `knowledge/patch_quality_audit_v2_2026-09.md`.
- **Localization/fix benchmarks** (historical, offline): 93% top-1 file
  localization vs 11% for the discarded keyword system; 49% of fixes
  judged equivalent to the merged human fix; 4/5 decisive patches passed
  maintainers' own tests. Benchmarks, not production claims.
- **Durability**: all five earliest merged patches survived upstream at
  last offline measurement (seam divergence normal); a prospective
  tracker (d3) now watches every merge, with the first honest
  longitudinal read expected ~2027-02.

## First-order caveats

- All merged work to date shipped through the operator; autonomous
  shipping has existed in production since 2026-08-04 but produced its
  first PR only on 2026-09-01 — capacity limits, gates, and policy
  routing (correctly) consumed the earlier opportunities.
- Maintainer relationships are few and young; the strongest evidence
  (caddy) is also where a maintainer explicitly required human-authored
  interaction — which is policy-encoded, and a real constraint on what
  "autonomous" can ever mean there.
- Nothing in this record supports extrapolation beyond: Go-ecosystem
  bug-fix-class issues, in repositories whose policies Omega has
  audited, at low volume, under continuous operator governance.
