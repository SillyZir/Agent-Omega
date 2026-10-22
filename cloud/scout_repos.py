"""Repository Scout — finds and scores candidate repos for contribution.

Automates the responsiveness screen that previously lived in a session-local
script: for each candidate repo, ONE GraphQL call fetches activity, backlog,
CI health of recent default-branch commits, maintainer engagement on open
issues, and the last N merged/closed PRs from which external-contributor
responsiveness is computed (median hours to first maintainer response,
% responded, merged:closed-unmerged ratio — the metrics that historically
surfaced rclone 57%/18h/11:1 and caddy 61%/14h/12:2).

Scout v2 (2026-08-06) adds seven signals on top of that. Six of them are pure
derivations from bytes the v1 query ALREADY paid for and discarded — merge
latency, external-vs-bot contributor share, true maintainer activity (distinct
maintainer logins acting in 90d, replacing `pushedAt`, which a release bot can
fake), issue freshness, well-specified-bug-report rate, and a backlog rescaled
per active maintainer. The seventh group (test suite, contributor docs, CI
accessibility, and the CLA/DCO/AI-disclosure/changelog burdens) needs file
contents and lives behind the two-stage gate in scout_probe.py.

Fail-honest: a repo whose metrics cannot be fully fetched is reported under
errors and never scored on partial data; repos with too few external PRs are
reported as "insufficient sample", never scored. A signal that cannot be
computed is OMITTED from the weighted mean, never defaulted to zero — absence
of evidence must not read as evidence of badness.
"""

import datetime
import statistics

import scout_probe
from scout_gh import (ScoutFetchError, gh_graphql, gh_search_repos,
                      is_bot, is_maintainer)
# Bug-report quality reuses the issue scout's repro patterns verbatim rather
# than authoring a second, silently-diverging set.
from scout_issues import _REPRO

_REPO_QUERY = """
query($owner:String!,$name:String!,$prSample:Int!){
 repository(owner:$owner,name:$name){
  pushedAt stargazerCount isArchived isFork
  primaryLanguage{name}
  issues(states:OPEN){totalCount}
  defaultBranchRef{target{... on Commit{
   history(first:10){nodes{statusCheckRollup{state}}}}}}
  pullRequests(last:$prSample,states:[MERGED,CLOSED]){nodes{
   state createdAt mergedAt authorAssociation author{login}
   comments(first:10){nodes{createdAt authorAssociation author{login}}}
   reviews(first:5){nodes{submittedAt authorAssociation author{login}}}}}
  openIssuesSample: issues(last:20,states:OPEN){nodes{
   createdAt updatedAt body
   comments(first:10){nodes{createdAt authorAssociation author{login}}}}}
 }}
"""

# Windows and floors for the v2 derivations. Named here rather than inlined so
# the backtest and the unit tests reference the same numbers as production.
ACTIVE_WINDOW_DAYS = 90      # "recent maintainer activity"
FRESH_WINDOW_DAYS = 30       # "issue freshness"
MERGE_LATENCY_CEILING_H = 336.0   # 14 days -> subscore 0
ISSUES_PER_MAINTAINER_OK = 60     # backlog_v2 -> 1.0 at or below this
ISSUES_PER_MAINTAINER_MAX = 600   # backlog_v2 bottoms out (at the floor) here
BACKLOG_FLOOR = 0.5          # see _backlog_v2_subscore
MIN_ISSUE_BODY_CHARS = 200   # below this a report cannot carry a repro

# Distinct active maintainers -> subscore. Saturating, NOT linear, and
# calibrated against rclone in the 2026-08-06 backtest: rclone has exactly one
# active maintainer (ncw), an 979-issue backlog, and it MERGED our PR while
# answering in 28h. A linear count/5 scored that 0.2 and, combined with a
# per-maintainer backlog, charged the repo twice for the single fact of being a
# one-maintainer project. This signal answers "is anyone home?" — which one
# responsive maintainer answers — while HOW responsive they are is already
# measured directly by resp_time / resp_rate / merge_latency.
_MAINTAINER_CURVE = {0: 0.0, 1: 0.6, 2: 0.8}
MAINTAINER_SATURATION = 3    # 3+ distinct active maintainers -> 1.0


def _clamp01(x):
    return max(0.0, min(1.0, x))


def _parse_ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _hours_between(a, b):
    return (_parse_ts(b) - _parse_ts(a)).total_seconds() / 3600.0


def responsiveness_from_prs(pr_nodes, min_external):
    """Responsiveness metrics over external-author PRs only."""
    externals, first_hours, responded = [], [], 0
    merged = closed_unmerged = 0
    for pr in pr_nodes or []:
        login = ((pr.get("author") or {}).get("login")) or ""
        if is_maintainer(pr.get("authorAssociation")) or is_bot(login):
            continue
        externals.append(pr)
        if pr.get("state") == "MERGED":
            merged += 1
        else:
            closed_unmerged += 1
        events = [(c.get("createdAt"), c.get("authorAssociation"),
                   (c.get("author") or {}).get("login"))
                  for c in (pr.get("comments") or {}).get("nodes") or []]
        events += [(r.get("submittedAt"), r.get("authorAssociation"),
                    (r.get("author") or {}).get("login"))
                   for r in (pr.get("reviews") or {}).get("nodes") or []]
        maint = sorted(ts for ts, assoc, lg in events
                       if ts and is_maintainer(assoc) and lg != login and not is_bot(lg))
        if maint:
            responded += 1
            first_hours.append(_hours_between(pr["createdAt"], maint[0]))
    if len(externals) < min_external:
        return {"insufficient_sample": True, "external_sample": len(externals)}
    return {
        "external_sample": len(externals),
        "median_first_response_hours": (round(statistics.median(first_hours), 1)
                                        if first_hours else None),
        "pct_responded": round(responded / len(externals), 2),
        "merged": merged,
        "closed_unmerged": closed_unmerged,
    }


def pr_derivations_v2(pr_nodes, now):
    """v2 signals from the PR sample the v1 query already fetched.

    Kept separate from responsiveness_from_prs() so that function's output —
    and therefore score_repo()'s — stays byte-identical to v1.
    """
    cutoff = now - datetime.timedelta(days=ACTIVE_WINDOW_DAYS)
    merge_hours, ext_authors, all_authors, bot_authors = [], set(), set(), set()
    active_maintainers = set()
    for pr in pr_nodes or []:
        login = ((pr.get("author") or {}).get("login")) or ""
        external = not is_maintainer(pr.get("authorAssociation"))
        if login:
            if is_bot(login):
                bot_authors.add(login)
            else:
                all_authors.add(login)
                if external:
                    ext_authors.add(login)
                    if pr.get("state") == "MERGED" and pr.get("mergedAt"):
                        merge_hours.append(
                            _hours_between(pr["createdAt"], pr["mergedAt"]))
        events = [(c.get("createdAt"), c.get("authorAssociation"),
                   (c.get("author") or {}).get("login"))
                  for c in (pr.get("comments") or {}).get("nodes") or []]
        events += [(r.get("submittedAt"), r.get("authorAssociation"),
                    (r.get("author") or {}).get("login"))
                   for r in (pr.get("reviews") or {}).get("nodes") or []]
        for ts, assoc, lg in events:
            if ts and lg and is_maintainer(assoc) and not is_bot(lg):
                if _parse_ts(ts) >= cutoff:
                    active_maintainers.add(lg)
    out = {"active_maintainers": sorted(active_maintainers),
           "bot_author_count": len(bot_authors)}
    if merge_hours:
        out["median_merge_hours"] = round(statistics.median(merge_hours), 1)
    if all_authors or bot_authors:
        # "external contributors vs bots": of the distinct humans and bots
        # opening PRs here, what share are outside contributors?
        denom = len(all_authors) + len(bot_authors)
        out["external_share"] = round(len(ext_authors) / denom, 2)
    return out


def issue_derivations_v2(issue_nodes, now):
    """Freshness and bug-report quality over the sampled open issues."""
    nodes = issue_nodes or []
    if not nodes:
        return {}
    fresh_cutoff = now - datetime.timedelta(days=FRESH_WINDOW_DAYS)
    fresh = specified = 0
    for i in nodes:
        upd = i.get("updatedAt")
        if upd and _parse_ts(upd) >= fresh_cutoff:
            fresh += 1
        body = i.get("body") or ""
        if len(body) >= MIN_ISSUE_BODY_CHARS and _REPRO.search(body):
            specified += 1
    return {"issue_sample": len(nodes),
            "issue_freshness": round(fresh / len(nodes), 2),
            "issue_quality": round(specified / len(nodes), 2)}


def _backlog_subscore(open_issues):
    if open_issues < 20:
        return 0.0
    if 30 <= open_issues <= 500:
        return 1.0
    if open_issues <= 1500:
        return 0.8
    return 0.5


def _maintainer_subscore(n):
    """Saturating: presence of maintainers, not headcount. See _MAINTAINER_CURVE."""
    if n is None:
        return None
    return 1.0 if n >= MAINTAINER_SATURATION else _MAINTAINER_CURVE[n]


def _backlog_v2_subscore(open_issues, n_maintainers):
    """Backlog per active maintainer, not in absolute terms.

    v1 charged a 2000-issue project the same 0.5 whether it had two burnt-out
    maintainers or twenty active ones. Returns None when maintainer activity
    is unknown, so the caller omits the signal rather than guessing.

    The ramp is deliberately gentle and floored at BACKLOG_FLOOR. A large
    backlog is genuinely ambiguous for us — it means more work to choose from
    as much as it means neglect — and the thing we actually care about,
    whether maintainers still get to their queue, is measured directly by
    resp_time / resp_rate / merge_latency rather than inferred from a ratio.
    Where the inference and the measurement disagree, the measurement wins;
    rclone is the case that proved it (979 issues, one maintainer, 1.8d
    median merge, our PR merged).
    """
    if n_maintainers is None:
        return None          # unknown — omit the signal, do not guess
    if open_issues < 20:
        return 0.0
    if n_maintainers == 0:
        # MEASURED zero, not unknown: we sampled 40 PRs and 20 issues and found
        # no maintainer acting in 90 days. A backlog nobody is working is the
        # worst case, not a missing datum. Conflating the two (the `not
        # n_maintainers` test this replaces) let lonng/nano — 0 active
        # maintainers, 5% of issues touched in 30d — reach a proposal at 0.64
        # in the 2026-08-06 dry run, because omitting the signal is no penalty.
        return 0.0
    per = open_issues / n_maintainers
    if per <= ISSUES_PER_MAINTAINER_OK:
        return 1.0
    span = ISSUES_PER_MAINTAINER_MAX - ISSUES_PER_MAINTAINER_OK
    decay = min(1.0, (per - ISSUES_PER_MAINTAINER_OK) / span)
    return round(1.0 - (1.0 - BACKLOG_FLOOR) * decay, 3)


def repo_metrics(repo, cfg, budget):
    """Fetch all scoring inputs for one repo in a single GraphQL call."""
    owner, name = repo.split("/", 1)
    r = gh_graphql(_REPO_QUERY, budget, owner=owner, name=name,
                   prSample=cfg["responsiveness"]["pr_sample"])["repository"]
    if r is None:
        raise ScoutFetchError("repository not found")
    now = datetime.datetime.now(datetime.timezone.utc)
    pushed = datetime.datetime.fromisoformat(r["pushedAt"].replace("Z", "+00:00"))
    history = (((r.get("defaultBranchRef") or {}).get("target") or {})
               .get("history") or {}).get("nodes") or []
    rollups = [(c.get("statusCheckRollup") or {}).get("state")
               for c in history if c.get("statusCheckRollup")]
    engaged = [i for i in (r.get("openIssuesSample") or {}).get("nodes") or []
               if any(is_maintainer(c.get("authorAssociation"))
                      for c in (i.get("comments") or {}).get("nodes") or [])]
    sample = (r.get("openIssuesSample") or {}).get("nodes") or []
    pr_nodes = (r.get("pullRequests") or {}).get("nodes")
    resp = responsiveness_from_prs(pr_nodes, cfg["responsiveness"]["min_external_prs"])
    # v2: same bytes, more signal. Maintainer activity also counts maintainers
    # who only ever answer issues — a real pattern in triage-heavy projects.
    v2 = pr_derivations_v2(pr_nodes, now)
    issue_cutoff = now - datetime.timedelta(days=ACTIVE_WINDOW_DAYS)
    extra_maint = {lg for i in sample
                   for c in (i.get("comments") or {}).get("nodes") or []
                   for lg in [(c.get("author") or {}).get("login")]
                   if lg and is_maintainer(c.get("authorAssociation"))
                   and not is_bot(lg) and c.get("createdAt")
                   and _parse_ts(c["createdAt"]) >= issue_cutoff}
    v2["active_maintainers"] = sorted(set(v2["active_maintainers"]) | extra_maint)
    v2["maintainer_count"] = len(v2["active_maintainers"])
    v2.update(issue_derivations_v2(sample, now))
    return {
        "stars": r["stargazerCount"],
        "language": (r.get("primaryLanguage") or {}).get("name") or "",
        "archived": r["isArchived"],
        "fork": r["isFork"],
        "open_issues": (r.get("issues") or {}).get("totalCount") or 0,
        "days_since_pushed": round((now - pushed).total_seconds() / 86400, 1),
        "ci_green_fraction": (round(sum(1 for s in rollups if s == "SUCCESS")
                                    / len(rollups), 2) if rollups else None),
        "engagement": (round(len(engaged) / len(sample), 2) if sample else None),
        **resp,
        **v2,
    }


def filter_repo(info, cfg):
    """Cheap search-result filter. Returns a reject reason or ''."""
    f = cfg["filters"]
    # Match the REPO name only, never the owner. "docs" in an org name, or
    # "book" in facebookgo/, says nothing about whether the repo is code, and
    # matching the full path made the pattern list both over- and under-eager.
    # Audit 2026-08-06 finding 8: 0 of 120 search results were rejected, and
    # three book/tutorial repos reached scoring.
    name = info["fullName"].lower().split("/", 1)[-1]
    if info.get("isArchived"):
        return "archived"
    if info.get("isFork"):
        return "fork"
    if info.get("stargazersCount", 0) < f["min_stars"]:
        return f"low_stars ({info.get('stargazersCount', 0)})"
    if (info.get("language") or "") not in f["languages"]:
        return f"language ({info.get('language') or 'unknown'})"
    if any(p in name for p in f["skip_name_patterns"]):
        return "non-code name pattern"
    return ""


def score_repo(metrics, weights):
    """Weighted mean of [0,1] subscores. Returns (score, breakdown, why)."""
    subs = {}
    med = metrics.get("median_first_response_hours")
    if med is not None:
        subs["resp_time"] = _clamp01(1 - med / 72.0)
    if metrics.get("pct_responded") is not None:
        subs["resp_rate"] = metrics["pct_responded"]
    total_closed = metrics.get("merged", 0) + metrics.get("closed_unmerged", 0)
    if total_closed:
        subs["merge_ratio"] = metrics["merged"] / total_closed
    subs["activity"] = _clamp01(1 - metrics["days_since_pushed"] / 30.0)
    subs["backlog"] = _backlog_subscore(metrics["open_issues"])
    if metrics.get("ci_green_fraction") is not None:
        subs["ci_health"] = metrics["ci_green_fraction"]
    if metrics.get("engagement") is not None:
        subs["engagement"] = metrics["engagement"]

    wsum = sum(weights[k] for k in subs)
    score = round(sum(weights[k] * v for k, v in subs.items()) / wsum, 2) if wsum else 0.0

    why = []
    if med is not None:
        why.append(f"first maintainer response median {med}h, "
                   f"{int(metrics['pct_responded'] * 100)}% of external PRs answered")
    if total_closed:
        why.append(f"merge ratio {metrics['merged']}:{metrics['closed_unmerged']}")
    if metrics.get("ci_green_fraction") is not None:
        why.append(f"CI {int(metrics['ci_green_fraction'] * 10)}/10 recent commits green")
    why.append(f"backlog {metrics['open_issues']} open issues, "
               f"pushed {metrics['days_since_pushed']}d ago")
    if metrics.get("engagement") is not None:
        why.append(f"maintainers active on {int(metrics['engagement'] * 100)}% "
                   "of sampled open issues")
    return score, {k: round(v, 2) for k, v in subs.items()}, why


def filter_metrics(metrics, cfg):
    """Post-fetch filter (v2). Returns a reject reason or ''.

    These are screening decisions, deliberately distinct from the burden
    penalties: a burden is a cost we can choose to pay and is therefore always
    surfaced with its price, whereas a repo nobody maintains is not a candidate
    at any price. Every rejection still carries an auditable reason —
    negative results are data.
    """
    f = cfg["filters"]
    if metrics["open_issues"] < f["min_open_issues"]:
        return f"low backlog ({metrics['open_issues']})"

    # Staleness stays a FILTER, not a subscore. v1 scored `activity` from
    # pushedAt at 1.0 of ~9 weight-units, which a single release-bot commit
    # could satisfy; v2 scores maintainer_activity instead and uses pushedAt
    # only to reject outright. `max_inactive_days` has sat in the config since
    # v1 and was read by nothing until 2026-08-06.
    max_inactive = f.get("max_inactive_days")
    if max_inactive and metrics["days_since_pushed"] > max_inactive:
        return (f"dormant (no push in {metrics['days_since_pushed']}d "
                f"> {max_inactive}d)")

    # The responsiveness subscores (weight 6.5 of the total) are computed over
    # the last N closed PRs — an UNBOUNDED time window. A repo that was
    # responsive in 2021 and dead since still scores well on all four.
    # maintainer_activity is the only 90d-bounded signal, so a measured zero
    # there must be decisive rather than one low subscore among many.
    # lonng/nano was the case that proved it: 68% historical response rate,
    # last push 161 days ago, 5% of issues touched in 30d, no maintainer action
    # in 90d — and it still reached 0.60 on the weighted mean alone. (v1 scored
    # it 0.65 and would have proposed it too.) If nobody has acted in 90 days
    # they cannot review our PR.
    min_maint = f.get("min_active_maintainers", 0)
    n = metrics.get("maintainer_count", 0)
    if min_maint and n < min_maint:
        return (f"no maintainer activity in {ACTIVE_WINDOW_DAYS}d "
                f"({n} < {min_maint}) — historical responsiveness "
                f"({int((metrics.get('pct_responded') or 0) * 100)}%) is not "
                f"current responsiveness")
    return ""


def _subscores_v2(metrics, probe):
    """Every v2 [0,1] subscore that can be computed, and only those.

    A signal with no evidence is left OUT of the dict; the weighted mean then
    renormalises over what is present. This is why a repo that has not been
    probed is not thereby penalised — it is scored on fewer axes, not on zeros.
    """
    subs, why = {}, []

    med = metrics.get("median_first_response_hours")
    if med is not None:
        subs["resp_time"] = _clamp01(1 - med / 72.0)
        why.append(f"first maintainer response median {med}h, "
                   f"{int(metrics['pct_responded'] * 100)}% of external PRs answered")
    if metrics.get("pct_responded") is not None:
        subs["resp_rate"] = metrics["pct_responded"]

    total_closed = metrics.get("merged", 0) + metrics.get("closed_unmerged", 0)
    if total_closed:
        subs["merge_ratio"] = metrics["merged"] / total_closed
        why.append(f"merge ratio {metrics['merged']}:{metrics['closed_unmerged']}")

    mh = metrics.get("median_merge_hours")
    if mh is not None:
        subs["merge_latency"] = _clamp01(1 - mh / MERGE_LATENCY_CEILING_H)
        why.append(f"external PRs merge in {round(mh / 24.0, 1)}d median")

    if metrics.get("external_share") is not None:
        subs["external_share"] = metrics["external_share"]
        why.append(f"{int(metrics['external_share'] * 100)}% of PR authors are "
                   f"external humans ({metrics.get('bot_author_count', 0)} bot accounts)")

    n_maint = metrics.get("maintainer_count")
    if n_maint is not None:
        subs["maintainer_activity"] = _maintainer_subscore(n_maint)
        why.append(f"{n_maint} distinct maintainer(s) active in "
                   f"{ACTIVE_WINDOW_DAYS}d")

    backlog = _backlog_v2_subscore(metrics.get("open_issues", 0), n_maint)
    if backlog is not None:
        subs["backlog"] = backlog
        if n_maint:
            why.append(f"backlog {metrics['open_issues']} open "
                       f"(~{round(metrics['open_issues'] / n_maint)} per active maintainer)")

    if metrics.get("issue_freshness") is not None:
        subs["issue_freshness"] = metrics["issue_freshness"]
    if metrics.get("issue_quality") is not None:
        subs["issue_quality"] = metrics["issue_quality"]
        # Each signal contributes its own clause. Reading issue_freshness from
        # inside the issue_quality branch coupled two independently-omittable
        # signals; a metrics dict carrying one without the other raised an
        # uncaught KeyError, and neither scout_repos (ScoutFetchError) nor
        # scout_run (BudgetExceeded) catches it — it would take down the whole
        # scout stage. Audit 2026-08-06 finding 9.
        note = (f"{int(metrics['issue_quality'] * 100)}% of sampled open issues "
                f"carry a repro")
        if metrics.get("issue_freshness") is not None:
            note += (f"; {int(metrics['issue_freshness'] * 100)}% touched "
                     f"in {FRESH_WINDOW_DAYS}d")
        why.append(note)

    if metrics.get("ci_green_fraction") is not None:
        subs["ci_health"] = metrics["ci_green_fraction"]
        why.append(f"CI {int(metrics['ci_green_fraction'] * 10)}/10 recent commits green")
    if metrics.get("engagement") is not None:
        subs["engagement"] = metrics["engagement"]

    for key in ("test_suite", "contributor_docs", "ci_accessible"):
        val = (probe or {}).get(key)
        if val is not None:
            subs[key] = _clamp01(val)
    if probe and probe.get("test_density") is not None:
        why.append(f"test density {probe['test_density']} "
                   f"(_test.go per .go), CI workflows "
                   f"{'present' if probe.get('ci_accessible') else 'absent'}")
    return subs, why


def score_repo_v2(metrics, weights, probe=None, burden_penalties=None):
    """Scout v2 score. Returns (score, breakdown, why, burdens).

    Shape is deliberately v1's — a config-driven weighted mean of [0,1]
    subscores — so weights stay operator-tunable in JSON. The one addition is
    that contribution burdens are subtracted AFTER the mean and each one is
    named in `burdens`. They are never a filter: a high-burden repo can still
    clear the threshold and is proposed with its cost written out, which is how
    cilium was admitted by hand.
    """
    subs, why = _subscores_v2(metrics, probe)
    wsum = sum(weights[k] for k in subs if k in weights)
    base = (sum(weights[k] * v for k, v in subs.items() if k in weights) / wsum
            if wsum else 0.0)

    burdens, penalty = [], 0.0
    for name, cost in sorted((burden_penalties or {}).items()):
        if (probe or {}).get(name):
            burdens.append({"burden": name, "penalty": round(cost, 3),
                            "evidence": (probe.get("burden_evidence") or {}).get(name, "")})
            penalty += cost
    if burdens:
        why.append("burdens: " + ", ".join(
            f"{b['burden']} (-{b['penalty']})" for b in burdens))
    if not probe:
        why.append("not probed — scored on API signals only")

    score = round(_clamp01(base - penalty), 2)
    return score, {k: round(v, 2) for k, v in subs.items()}, why, burdens


def _reject_entry(prior, today, status, reason):
    """State entry for a repo examined and turned away.

    Every terminal outcome is persisted, not just the ones that produce a score.
    Rejections used to be appended to the report's `examined` list and dropped
    on the floor, so a dormant or low-traffic repo was re-fetched EVERY cycle
    forever, consuming one API call and one slot of the daily scoring cap each
    time. Ten days of running had persisted 18 repos out of a 120-repo search
    pool; lonng/nano, kubernetes/minikube and gohugoio/hugo were each being
    re-examined daily with the same answer. The frontier could only advance by
    the number of repos that passed per cycle (~1.8), so discovery was
    asymptotically starving. Audit 2026-08-06 finding 2.

    `last_scored` is set so the normal repropose_days skip applies uniformly —
    a rejection is re-checked on the same cadence as a score, because a dormant
    repo can revive.
    """
    return {"first_seen": (prior or {}).get("first_seen", today.isoformat()),
            "last_scored": today.isoformat(), "status": status,
            "reason": reason, "score": None, "scorer": "v2"}


def scout_repos(cfg, state, exclusions, budget, log):
    """Search ecosystems, filter, score, and propose. Returns report data."""
    out = {"proposed": [], "examined": [], "errors": [], "searched": 0,
           "probes": 0, "probe_errors": []}
    if not cfg.get("enabled", True):
        return out
    today = datetime.date.today()
    known = state.setdefault("repos", {})
    scored_today = 0
    probed_today = 0
    seen = set()
    probe_cache = scout_probe.load_cache()
    cache_dirty = False
    for eco in cfg["ecosystems"]:
        if not eco.get("enabled", True):
            continue
        for query in eco["queries"]:
            try:
                results = gh_search_repos(query, eco["search_limit"], budget)
            except ScoutFetchError as e:
                out["errors"].append(f"search '{query}' failed: {e}")
                continue
            out["searched"] += len(results)
            for info in results:
                repo = info["fullName"]
                if repo in seen:
                    continue
                seen.add(repo)
                reason = filter_repo(info, cfg)
                if reason:
                    out["examined"].append({"repo": repo, "outcome": "filtered",
                                            "reason": reason})
                    continue
                # Denial precedes the softer skips: a standing operator ruling
                # must not be masked by "already engaged" or a repropose-days
                # window, and it is named in the report EVERY cycle rather than
                # dropped — an invisible ruling is indistinguishable from a bug.
                if repo in exclusions.get("denied", ()):
                    out["examined"].append({"repo": repo, "outcome": "denied",
                                            "reason": "operator ruling — see denied_repos_note"})
                    continue
                if repo in exclusions["engaged"]:
                    out["examined"].append({"repo": repo, "outcome": "skipped",
                                            "reason": "already engaged (open tracked PRs)"})
                    continue
                if repo in exclusions["approved"]:
                    out["examined"].append({"repo": repo, "outcome": "skipped",
                                            "reason": "already approved for issue scouting"})
                    continue
                prior = known.get(repo)
                if prior and prior.get("last_scored"):
                    age = (today - datetime.date.fromisoformat(prior["last_scored"])).days
                    if age < cfg["repropose_days"]:
                        prev = (f"score {prior['score']}"
                                if prior.get("score") is not None
                                else prior.get("status") or "not scored")
                        out["examined"].append({"repo": repo, "outcome": "skipped",
                                                "reason": f"examined {age}d ago ({prev})"})
                        continue
                if scored_today >= cfg.get("max_new_repos_scored_per_day", 10):
                    out["examined"].append({"repo": repo, "outcome": "not examined",
                                            "reason": "daily scoring cap reached"})
                    continue
                # Count the ATTEMPT, not the success. repo_metrics spends the
                # budget before it can fail, so incrementing afterwards let a
                # repo that always fails burn a call every cycle without ever
                # consuming a slot — the cap bounded successes while the budget
                # bounded nothing. Audit 2026-08-06 finding 10.
                scored_today += 1
                try:
                    metrics = repo_metrics(repo, cfg, budget)
                except ScoutFetchError as e:
                    # Not persisted: a fetch failure is transient and says
                    # nothing about the repo. It costs a slot, not a verdict.
                    out["errors"].append(f"{repo} — metrics fetch failed: {e}")
                    continue
                if metrics.get("insufficient_sample"):
                    reason = (f"insufficient external-PR sample "
                              f"({metrics['external_sample']})")
                    known[repo] = _reject_entry(prior, today,
                                                "insufficient_sample", reason)
                    out["examined"].append({"repo": repo, "outcome": "not scored",
                                            "reason": reason})
                    continue
                reason = filter_metrics(metrics, cfg)
                if reason:
                    known[repo] = _reject_entry(prior, today, "rejected", reason)
                    out["examined"].append({"repo": repo, "outcome": "filtered",
                                            "reason": reason})
                    continue
                # v1 score is still computed and recorded alongside v2. It is
                # free (pure arithmetic on metrics already in hand) and it is
                # the only way to measure what the cutover actually changed.
                score_v1, breakdown_v1, _ = score_repo(metrics, cfg["weights"])

                # Two-stage gate: the API-only score decides whether the repo
                # is worth two more calls. probe_threshold sits BELOW
                # propose_threshold on purpose — a repo the probe could lift
                # over the bar must not be excluded by the cheap pass.
                weights2 = cfg["weights_v2"]
                penalties = cfg.get("burden_penalties", {})
                pre, _, _, _ = score_repo_v2(metrics, weights2)
                probe, probe_status = None, "not probed (below probe_threshold)"
                if pre >= cfg.get("probe_threshold", 0.55):
                    if probed_today >= cfg.get("max_probes_per_day", 6):
                        probe_status = "not probed (daily probe cap reached)"
                    else:
                        probe, probe_status = scout_probe.probe_with_cache(
                            repo, probe_cache, budget, cfg, today)
                        if probe_status.startswith("probed"):
                            probed_today += 1
                            out["probes"] += 1
                            # Only a COMPLETE probe is cached; see probe_with_cache.
                            cache_dirty = cache_dirty or probe_status == "probed"
                        elif probe_status.startswith("error"):
                            out["probe_errors"].append(f"{repo} — probe failed: "
                                                       f"{probe_status[7:]}")
                # A failed tree half is an incomplete measurement, not a clean
                # one: test_suite and ci_accessible are absent and the report
                # still said "probed". Surfacing it here is what keeps
                # probe_status honest. Audit 2026-08-06 finding 6.
                if probe and probe.get("tree_error"):
                    out["probe_errors"].append(
                        f"{repo} — probe incomplete: tree unreadable "
                        f"({probe['tree_error']}); scored without "
                        f"test_suite/ci_accessible")
                score, breakdown, why, burdens = score_repo_v2(
                    metrics, weights2, probe, penalties)

                # A repo that clears the bar WITHOUT having been probed has not
                # had its burdens checked, and "burdens: none detected" would
                # render an absence of measurement as a measurement. The same
                # synthetic repo scored 0.73 unprobed and 0.58 probed — a 0.15
                # swing across a 0.60 threshold decided by nothing but how many
                # repos happened to precede it in the scan. Hold it; the next
                # cycle probes it. Audit 2026-08-06 finding 1.
                if score >= cfg["propose_threshold"] and probe is None:
                    # No last_scored: deferral must be retried on the NEXT
                    # cycle, not suppressed for repropose_days.
                    known[repo] = {
                        "first_seen": (prior or {}).get("first_seen", today.isoformat()),
                        "last_deferred": today.isoformat(), "status": "deferred",
                        "reason": probe_status, "score": score,
                        "score_v1": score_v1, "scorer": "v2"}
                    out["examined"].append({
                        "repo": repo, "outcome": "deferred",
                        "reason": f"v2 score {score} clears threshold but "
                        f"{probe_status} — burdens unverified, held for next cycle"})
                    continue

                entry = {"first_seen": (prior or {}).get("first_seen", today.isoformat()),
                         "last_scored": today.isoformat(), "score": score,
                         "score_v1": score_v1, "scorer": "v2",
                         "burdens": [b["burden"] for b in burdens],
                         "probe_status": probe_status,
                         "status": "proposed" if score >= cfg["propose_threshold"]
                         else "below_threshold",
                         "breakdown": breakdown, "breakdown_v1": breakdown_v1,
                         "metrics": {k: v for k, v in metrics.items()
                                     if k != "insufficient_sample"}}
                # Rejected/insufficient_sample entries are remembered with
                # score None; .get's default only covers an ABSENT key, so
                # the first re-examined rejection crashed the whole scan
                # (in-26, 2026-08-21). A None prior has no comparable score
                # -> treat as moved.
                prior_score = (prior or {}).get("score")
                moved = (prior_score is None
                         or abs(score - prior_score) >= 0.05)
                known[repo] = entry
                if score >= cfg["propose_threshold"] and (not prior or moved
                                                          or prior.get("status") != "proposed"):
                    profiled = repo in exclusions["profiled"]
                    out["proposed"].append({"repo": repo, "score": score,
                                            "score_v1": score_v1,
                                            "breakdown": breakdown, "why": why,
                                            "burdens": burdens,
                                            "probe_status": probe_status,
                                            "known": profiled})
                    log(f"repo scout: proposing {repo} (v2 {score}, v1 {score_v1}"
                        + (f", burdens: {', '.join(b['burden'] for b in burdens)}"
                           if burdens else "") + ")")
                else:
                    out["examined"].append({"repo": repo, "outcome": "scored",
                                            "reason": f"v2 score {score} "
                                            f"{'≥' if score >= cfg['propose_threshold'] else '<'} "
                                            f"threshold {cfg['propose_threshold']} "
                                            f"(v1 would have said {score_v1})"
                                            + ("; recently proposed" if prior else "")})
    if cache_dirty:
        try:
            scout_probe.save_cache(probe_cache)
        except OSError as e:
            out["errors"].append(f"probe cache not saved: {e}")
    return out
