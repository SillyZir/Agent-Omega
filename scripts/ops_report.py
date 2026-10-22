"""Operational reporting for the FROZEN v2.3.2 production baseline.

Read-only observability over the artifacts the pipeline already emits — it adds
NO production capability (the architecture is frozen; this is operation, not
expansion). Aggregates:

  * pipeline runs      — pipeline_v2_queue.jsonl (every run_production bundle)
  * learned knowledge  — repo_profiles/*.json (PR events, failure modes, prefs)
  * PR lifecycle       — with --refresh, the read-only PR monitor refreshes each
                         tracked PR's current state (and folds new events back
                         into the repo profiles).

Emits an operations report (ops_report.md) and appends a JSON snapshot to a
time-series log (ops_history.jsonl) so trends can be tracked across runs.

Usage:
  python3 scripts/ops_report.py            # from local artifacts
  python3 scripts/ops_report.py --refresh  # also refresh tracked PR states (gh)
"""

import collections
import datetime
import json
import os
import statistics
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from agent import repo_profile  # noqa: E402

QUEUE = os.path.join(_ROOT, "pipeline_v2_queue.jsonl")
REPORT = os.path.join(_ROOT, "ops_report.md")
HISTORY = os.path.join(_ROOT, "ops_history.jsonl")
TRACKED = os.path.join(_ROOT, "ops_tracked_prs.json")

# Pipeline outcomes that represent a non-shippable / failure disposition.
_FAILURE_OUTCOMES = ("checkout_failed", "pipeline_error", "no_fix", "reject",
                     "insufficient", "blocked_compliance", "ship_error",
                     "push_failed", "pr_create_failed", "changelog_failed")


def load_runs():
    """(latest bundle per issue, outcome counter over ALL lines).

    The deduped view answers "where does each issue stand NOW"; the all-lines
    counter answers "what EVENTS happened" — a compliance stop that was later
    resolved by a re-run must still count as a stop (audit: caddy#6435's
    blocked_compliance was erased by its own successful re-run, and the report
    claimed the gate never fired)."""
    by, all_outcomes = {}, collections.Counter()
    if os.path.isfile(QUEUE):
        for line in open(QUEUE):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            by[f"{r.get('repo')}#{r.get('number')}"] = r
            all_outcomes[r.get("outcome")] += 1
    return list(by.values()), all_outcomes


def load_profiles():
    profs = []
    d = repo_profile.PROFILE_DIR
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.endswith(".json"):
                try:
                    profs.append(json.load(open(os.path.join(d, f))))
                except (json.JSONDecodeError, OSError):
                    continue
    return profs


def tracked_prs(runs, profs):
    """PRs we OPENED: shipped runs (have a pr_url) plus a manual seed
    (ops_tracked_prs.json — for PRs opened outside the pipeline). Deliberately
    NOT every PR we've *monitored* (audits monitor maintainers' PRs too), so the
    'PRs opened' count stays ours."""
    prs = set()
    for r in runs:
        # Only a run that actually opened a PR (has a pr_url) counts. A
        # "prepared" run built a local commit on auto-fix/<issue#> but never
        # pushed a PR — its branch number is the ISSUE, not a PR.
        s = r.get("ship") or {}
        if r.get("outcome") == "shipped" and s.get("pr_url"):
            num = s["pr_url"].rstrip("/").split("/")[-1]
            try:
                prs.add((r["repo"], int(num)))
            except (ValueError, TypeError):
                pass
    if os.path.isfile(TRACKED):
        try:
            for t in json.load(open(TRACKED)):
                prs.add((t["repo"], int(t["number"])))
        except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError):
            pass
    return sorted(prs)


def _pct(n, d):
    return f"{n} ({100 * n / d:.0f}%)" if d else str(n)


def _runtime(secs):
    if not secs:
        return "n/a"
    s = sorted(secs)
    p90 = s[max(0, min(len(s) - 1, round(0.9 * (len(s) - 1))))]
    return (f"mean {statistics.mean(s):.0f}s · median {statistics.median(s):.0f}s "
            f"· p90 {p90:.0f}s · max {max(s):.0f}s")


def collect(refresh=False):
    runs, all_oc = load_runs()
    profs = load_profiles()
    n = len(runs)
    oc = collections.Counter(r.get("outcome") for r in runs)

    # Event counters over ALL queue lines (history), not the deduped view — a
    # gate that fired and was later resolved still fired.
    dup = all_oc.get("skip_duplicate", 0)
    comp = all_oc.get("blocked_compliance", 0)
    investigations = n - oc.get("skip_duplicate", 0)  # issues that reached the agent
    fixes = sum(1 for r in runs if (r.get("diff") or "").strip())
    verdicts = collections.Counter(
        (r.get("review") or {}).get("verdict") for r in runs if r.get("review"))
    secs = [r.get("seconds") or r.get("wall_seconds") or 0 for r in runs]
    secs = [x for x in secs if x]
    ship_ready = sum(1 for r in runs if r.get("action") == "review"
                     and (r.get("test") or {}).get("passed") is True)

    prs = tracked_prs(runs, profs)
    pr_states = collections.Counter()
    pr_events = collections.Counter()
    if refresh:
        from scripts import pr_monitor
        for repo, num in prs:
            # One flaky gh call (timeout, non-JSON stdout, missing field) must
            # not kill the whole report run (audit finding).
            try:
                m = pr_monitor.monitor_pr(repo, num)  # read-only + folds learnings
            except Exception as e:
                m = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
            if m.get("error"):
                pr_states["error"] += 1
                continue
            pr_states[m.get("state", "?")] += 1
            for e in m.get("events", []):
                pr_events[e["class"]] += 1
        profs = load_profiles()                    # reload post-learn
    else:
        for p in profs:
            for e in p.get("learned", {}).get("events", []):
                pr_events[e["class"]] += 1

    failure_modes, prefs = [], []
    for p in profs:
        failure_modes += p.get("learned", {}).get("failure_modes", [])
        prefs += p.get("learned", {}).get("maintainer_prefs", [])
    fail_outcomes = {k: v for k, v in oc.items() if k in _FAILURE_OUTCOMES}

    return {
        "runs": n, "outcomes": dict(oc), "duplicate_skips": dup,
        "compliance_stops": comp, "investigations": investigations,
        "fixes_generated": fixes, "review_verdicts": dict(verdicts),
        "ship_ready": ship_ready, "runtime_secs": secs,
        "prs_tracked": len(prs), "pr_states": dict(pr_states),
        "pr_events": dict(pr_events),
        "requested_changes": pr_events.get("requested changes", 0),
        "approvals": pr_events.get("approval", 0),
        "merges": pr_events.get("merge", 0),
        "closes": pr_events.get("close", 0),
        "failure_modes": sorted(set(failure_modes)),
        "fail_outcomes": fail_outcomes,
        "maintainer_prefs": prefs[:10],
        "repos": sorted({r.get("repo") for r in runs}),
    }


def render(m, ts):
    L = [f"# Omega — Operations Report", "",
         f"Generated {ts} · frozen **v2.3.x** baseline line · read-only.", ""]
    L += ["## Pipeline activity",
          f"- issues processed: **{m['runs']}** across {len(m['repos'])} repos",
          f"- investigations (reached the agent): **{m['investigations']}**",
          f"- duplicate skips: **{m['duplicate_skips']}**",
          f"- compliance stops: **{m['compliance_stops']}**",
          f"- fixes generated (patch produced): {_pct(m['fixes_generated'], m['runs'])}",
          f"- ship-ready (review + tests pass): {_pct(m['ship_ready'], m['runs'])}",
          f"- runtime/issue: {_runtime(m['runtime_secs'])}", ""]
    L += ["## Outcome distribution"]
    for k, v in sorted(m["outcomes"].items(), key=lambda x: -x[1]):
        L.append(f"- `{k}`: {_pct(v, m['runs'])}")
    L += ["", "## Review verdicts (production reviewer)"]
    tot = sum(m["review_verdicts"].values())
    for k, v in sorted(m["review_verdicts"].items(), key=lambda x: -x[1]):
        L.append(f"- `{k}`: {_pct(v, tot)}")
    L += ["", "## Contribution lifecycle (opened PRs)",
          f"- PRs tracked: **{m['prs_tracked']}**"
          + (f" — states {m['pr_states']}" if m["pr_states"] else " (states via --refresh)"),
          f"- maintainer events observed: {m['pr_events'] or '(run --refresh)'}",
          f"- requested changes: {m['requested_changes']} · approvals: {m['approvals']}"
          f" · merges: {m['merges']} · closes: {m['closes']}", ""]
    L += ["## Recurring failure modes"]
    for fm in m["failure_modes"]:
        L.append(f"- {fm}")
    if m["fail_outcomes"]:
        L.append(f"- non-shippable outcomes: {m['fail_outcomes']}")
    if not m["failure_modes"] and not m["fail_outcomes"]:
        L.append("- (none recorded)")
    if m["maintainer_prefs"]:
        L += ["", "## Maintainer feedback (learned)"]
        for p in m["maintainer_prefs"]:
            L.append(f"- {p[:160]}")
    return "\n".join(L) + "\n"


def main():
    refresh = "--refresh" in sys.argv
    now = datetime.datetime.now().astimezone()
    m = collect(refresh=refresh)
    open(REPORT, "w").write(render(m, now.isoformat(timespec="seconds")))
    snap = {"ts": now.isoformat(timespec="seconds"),
            **{k: m[k] for k in ("runs", "investigations", "duplicate_skips",
                                 "compliance_stops", "fixes_generated",
                                 "ship_ready", "prs_tracked", "pr_states",
                                 "requested_changes", "approvals", "merges",
                                 "closes", "outcomes", "review_verdicts")}}
    with open(HISTORY, "a") as f:
        f.write(json.dumps(snap) + "\n")
    print(open(REPORT).read())
    print(f"[snapshot appended -> {os.path.basename(HISTORY)}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
