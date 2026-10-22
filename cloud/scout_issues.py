"""Issue Scout — discovers, filters, and ranks candidate issues in the
operator-approved repo allowlist.

Deterministic scoring, no LLM calls: hard filters remove issues that are
assigned, locked, label-excluded (enhancement/design/question/good-first-issue
spam), stale, too fresh, maintainer-dismissed, or already seen; survivors get
a confidence score from evidence signals (maintainer confirmation, repro
evidence, single-subsystem scope per the in-4 600s triage ceiling, ...).
Top candidates are verified against agent/duplicate_check.py (the production
duplicate gate) before queueing — a candidate whose duplicate check errors is
excluded, never queued unverified.

Every exclusion carries an auditable reason: negative results are data.
"""

import datetime
import json
import os
import re
import sys

from scout_gh import ScoutFetchError, gh_graphql, is_bot, is_maintainer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from agent.duplicate_check import find_duplicates  # noqa: E402

_ISSUES_QUERY = """
query($owner:String!,$name:String!,$n:Int!){
 repository(owner:$owner,name:$name){
  issues(first:$n,states:OPEN,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{
   number title body createdAt updatedAt locked authorAssociation
   author{login}
   labels(first:20){nodes{name}}
   assignees(first:5){totalCount}
   comments(first:30){nodes{createdAt authorAssociation author{login} body}}
  }}
 }}
"""

_DISMISSAL = re.compile(r"duplicate of|can'?t reproduce|cannot reproduce|"
                        r"working as intended|not a bug|wontfix|won't fix",
                        re.IGNORECASE)
_REPRO = re.compile(r"steps to reproduce|how to reproduce|```|panic:|"
                    r"goroutine \d|stack trace|expected.{0,400}(actual|got)",
                    re.IGNORECASE | re.DOTALL)
_FEATURE = re.compile(r"\badd support\b|\ballow\b|\boption to\b|\bhow to\b|"
                      r"\bshould (support|allow|add)\b|feature request|"
                      r"\bofficial\b.{0,40}\bsupport\b",
                      re.IGNORECASE)
# design/proposal issues are not fix candidates regardless of labels — the
# repo may not label them (observed 2026-07-31: fiber "[Proposal]: ..." and
# "v4: stop implementing context.Context" both slipped the label filter)
_PROPOSAL_TITLE = re.compile(r"\[?\b(proposal|rfc)\b\]?|\bredesign\b|"
                             r"\bstop (implementing|supporting)\b|"
                             r"\bbreaking change\b|\bdeprecate\b",
                             re.IGNORECASE)
_PATHISH = re.compile(r"\b[\w.-]+(?:/[\w.-]+)+\b")


def _days_ago(iso):
    t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 86400


def _safe_key(repo, number):
    return f"{repo} issue {number}"


def hard_filter(issue, cfg, seen_keys, repo):
    """Return an exclusion reason, or None if the issue survives."""
    labels = [(l.get("name") or "").lower()
              for l in (issue.get("labels") or {}).get("nodes") or []]
    if _safe_key(repo, issue["number"]) in seen_keys:
        return "already_seen"
    if cfg["require_no_assignee"] and (issue.get("assignees") or {}).get("totalCount", 0):
        return "assigned"
    if issue.get("locked"):
        return "locked"
    for lb in labels:
        # Batch B (2026-08-13): namespaced repos write `type/feature`,
        # `kind/enhancement` — match the suffix after the last `/` against the
        # same exclude list, whole-segment, so the exact-match asymmetry with
        # bug_label's substring bonus stops feeding feature requests through.
        if lb in cfg["exclude_labels"] or \
                lb.rsplit("/", 1)[-1] in cfg["exclude_labels"]:
            return f"excluded_label:{lb}"
    if _PROPOSAL_TITLE.search(issue.get("title") or ""):
        return "proposal_title (design request, not a fix candidate)"
    if _days_ago(issue["updatedAt"]) > cfg["stale_days"]:
        return "stale (no recent activity)"
    if _days_ago(issue["createdAt"]) > cfg["max_age_days"]:
        return "stale (too old)"
    if _days_ago(issue["createdAt"]) < cfg["min_age_days"]:
        return "too_fresh (no triage window yet)"
    author = ((issue.get("author") or {}).get("login")) or ""
    for c in (issue.get("comments") or {}).get("nodes") or []:
        lg = (c.get("author") or {}).get("login") or ""
        if (is_maintainer(c.get("authorAssociation")) and not is_bot(lg)
                and _DISMISSAL.search(c.get("body") or "")):
            return "maintainer_dismissed"
    if is_bot(author):
        return "bot_author"
    return None


def score_issue(issue, repo_score, weights):
    """Confidence in [0,1] from evidence signals. Returns (conf, breakdown, why)."""
    body = issue.get("body") or ""
    title = issue.get("title") or ""
    labels = [(l.get("name") or "").lower()
              for l in (issue.get("labels") or {}).get("nodes") or []]
    comments = (issue.get("comments") or {}).get("nodes") or []
    author = ((issue.get("author") or {}).get("login")) or ""
    breakdown, why = {}, []

    def hit(name, delta, reason):
        breakdown[name] = round(delta, 2)
        why.append(reason)

    maint = [c for c in comments
             if is_maintainer(c.get("authorAssociation"))
             and not is_bot((c.get("author") or {}).get("login"))
             and ((c.get("author") or {}).get("login")) != author
             and not _DISMISSAL.search(c.get("body") or "")]
    if maint:
        c = maint[0]
        hit("maintainer_confirmed", weights["maintainer_confirmed"],
            f"maintainer engaged: comment by {(c.get('author') or {}).get('login')} "
            f"({c.get('authorAssociation')}) {c.get('createdAt', '')[:10]}")
    if _REPRO.search(body):
        hit("repro_evidence", weights["repro_evidence"],
            "repro evidence in body (steps/code block/trace)")
    if any("bug" in lb or "confirmed" in lb for lb in labels):
        hit("bug_label", weights["bug_label"],
            f"bug-class label ({', '.join(lb for lb in labels if 'bug' in lb or 'confirmed' in lb)})")
    paths = set(_PATHISH.findall(title + " " + body[:4000]))
    paths = {p for p in paths if not p.startswith("http") and "." not in p.split("/")[0]}
    if (len(paths) <= 2 and 300 <= len(body) <= 6000
            and not re.search(r"refactor|several packages|across the", body, re.I)):
        hit("single_subsystem", weights["single_subsystem"],
            "well-scoped: single apparent subsystem, bounded description")
    if _days_ago(issue["updatedAt"]) <= 30:
        hit("recency", weights["recency"], "active in the last 30 days")
    if 1 <= len(comments) <= 15:
        hit("discussion_health", weights["discussion_health"],
            f"healthy discussion ({len(comments)} comments)")
    if repo_score:
        hit("repo_quality", weights["repo_quality"] * repo_score,
            f"responsive repo (score {repo_score})")
    if _FEATURE.search(title):
        hit("feature_language", weights["feature_language"],
            "title reads like a feature request")
    if len(comments) > 40:
        hit("contentious", weights["contentious"],
            f"contentious thread ({len(comments)} comments)")

    conf = max(0.0, min(1.0, 0.20 + sum(breakdown.values())))
    return round(conf, 2), breakdown, why


def _burden_note(repo):
    path = os.path.join(ROOT, "repo_profiles",
                        repo.replace("/", "__") + ".json")
    if not os.path.exists(path):
        return None
    try:
        prof = json.load(open(path))
    except Exception:
        return None
    reqs = prof.get("requirements") or prof
    flags = []
    for k in ("cla", "dco", "ai_disclosure", "changelog"):
        v = reqs.get(k)
        if isinstance(v, dict) and v.get("required"):
            flags.append(k.upper())
    return ("contribution burden: " + ", ".join(flags)) if flags else \
        "contribution burden: none known"


def scan_order(cfg):
    """Approved repos with operator-designated priority repos scanned first.

    Priority is an ordering, not an admission: a repo listed in priority_repos
    but absent from approved_repos is NOT scanned. Admission stays the single
    operator-gated act it has always been.
    """
    approved = list(cfg["approved_repos"])
    priority = [r for r in (cfg.get("priority_repos") or []) if r in approved]
    return priority + [r for r in approved if r not in priority]


def reserve_priority_slot(candidates, cfg, log=None):
    """Guarantee a priority repo the LAST daily slot if it would place nowhere.

    Reallocates an existing slot; never adds one. max_queue_per_day is an
    anti-spam control and does not move. The displaced candidate stays in the
    ranked list with status 'ranked' — it is deferred, not dropped, and its
    confidence rank is left honest rather than rewritten to justify the swap.

    Expects `candidates` already sorted by descending confidence with `rank`
    and `queued` assigned. Mutates in place; returns the promotion record or
    None.
    """
    cap = cfg["max_queue_per_day"]
    priority = set(cfg.get("priority_repos") or [])
    if not priority or cap < 1:
        return None
    queued = [c for c in candidates if c["queued"]]
    if any(c["repo"] in priority for c in queued):
        return None                     # already placed on merit; nothing to do
    promote = next((c for c in candidates
                    if c["repo"] in priority and not c["queued"]), None)
    if promote is None or len(queued) < cap:
        return None                     # no priority candidate, or a free slot
    displaced = queued[-1]              # lowest-confidence occupant
    displaced["queued"] = False
    displaced["displaced_by"] = promote["key"]
    promote["queued"] = True
    promote["promoted"] = "priority_repo reserved slot"
    rec = {"promoted": promote["key"], "confidence": promote["confidence"],
           "displaced": displaced["key"],
           "displaced_confidence": displaced["confidence"]}
    if log:
        log(f"issue scout: priority slot — queued {promote['key']} "
            f"({promote['confidence']}) in place of {displaced['key']} "
            f"({displaced['confidence']}); displaced candidate stays ranked")
    return rec


def scout_issues(cfg, state, open_pr_counts, repo_scores, seen_keys, budget, log,
                 cap_map=None):
    """Scan approved repos, filter/score/verify, return ranked candidates.

    seen_keys: safe keys ('owner/repo issue N') to skip — built by the caller
    from scout state (with re-examination expiry), the pending queue, and the
    pipeline outcome log.
    cap_map: adaptive per-repo capacity from cloud/repo_health.py (v2.5.0);
    empty/None falls back to the fixed over_concentration_limit."""
    out = {"candidates": [], "excluded": [], "repos_skipped": [], "errors": []}
    state.setdefault("issues", {})
    today = datetime.date.today().isoformat()

    for repo in scan_order(cfg):
        n_open = open_pr_counts.get(repo, 0)
        limit = (cap_map or {}).get(repo, cfg["over_concentration_limit"])
        if n_open >= limit:
            source = ("adaptive health capacity" if repo in (cap_map or {})
                      else "fixed fallback limit")
            out["repos_skipped"].append(
                {"repo": repo, "reason": f"at capacity ({n_open} open tracked "
                 f"PRs ≥ {limit}, {source})"})
            continue
        owner, name = repo.split("/", 1)
        try:
            nodes = gh_graphql(_ISSUES_QUERY, budget, owner=owner, name=name,
                               n=cfg["max_issues_per_repo"]
                               )["repository"]["issues"]["nodes"]
        except (ScoutFetchError, TypeError) as e:
            out["errors"].append(f"{repo} — issue fetch failed: {e}")
            continue
        survivors = []
        for issue in nodes:
            reason = hard_filter(issue, cfg, seen_keys, repo)
            key = _safe_key(repo, issue["number"])
            if reason:
                if reason != "already_seen":
                    out["excluded"].append({"key": key, "reason": reason})
                continue
            conf, breakdown, why = score_issue(issue, repo_scores.get(repo), cfg["weights"])
            if conf < cfg["min_confidence"]:
                out["excluded"].append({"key": key,
                                        "reason": f"low_confidence ({conf})"})
                continue
            survivors.append({"repo": repo, "number": issue["number"],
                              "title": issue["title"], "body": issue.get("body") or "",
                              "key": key, "confidence": conf,
                              "breakdown": breakdown, "why": why})
        survivors.sort(key=lambda s: -s["confidence"])
        for cand in survivors[:cfg["max_deep_checks_per_repo"]]:
            try:
                budget.spend(3)  # find_duplicates makes its own gh calls (~2-3)
                dup = find_duplicates(cand["repo"], cand["number"])
            except Exception as e:
                out["excluded"].append({"key": cand["key"],
                                        "reason": f"dup_check_unavailable ({str(e)[:80]})"})
                continue
            if dup.get("is_duplicate"):
                out["excluded"].append({"key": cand["key"],
                                        "reason": f"duplicate (confidence "
                                        f"{dup.get('confidence')})"})
                continue
            cand["duplicate_check"] = {"is_duplicate": False,
                                       "confidence": dup.get("confidence"),
                                       "signals": dup.get("signals", [])[:3]}
            cand["why"].append(f"clean timeline (duplicate check: proceed, "
                               f"confidence {dup.get('confidence')})")
            burden = _burden_note(cand["repo"])
            if burden:
                cand["why"].append(burden)
            out["candidates"].append(cand)
        log(f"issue scout: {repo} — {len(nodes)} fetched, "
            f"{len(survivors)} scored above threshold, "
            f"{len([c for c in out['candidates'] if c['repo'] == repo])} verified")

    out["candidates"].sort(key=lambda c: -c["confidence"])
    for i, cand in enumerate(out["candidates"], 1):
        cand["rank"] = i
        cand["queued"] = i <= cfg["max_queue_per_day"]
    promo = reserve_priority_slot(out["candidates"], cfg, log)
    if promo:
        out["priority_promotion"] = promo
    for cand in out["candidates"]:
        state["issues"][cand["key"]] = {
            "first_seen": today,
            "status": "queued" if cand["queued"] else "ranked",
            "confidence": cand["confidence"], "last_checked": today}
    for exc in out["excluded"]:
        state["issues"].setdefault(exc["key"], {"first_seen": today}).update(
            {"status": "excluded", "reason": exc["reason"], "last_checked": today})
    return out
