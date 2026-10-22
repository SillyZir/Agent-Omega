"""Duplicate-work detection — before investing an agentic session in an issue,
check whether it is already being addressed, and never knowingly submit
duplicate work.

Surfaced by production: terraform#38807 was closed by a maintainer because the
issue was "already being fixed by #38698". A cross-reference check catches that
before we spend a session or open a PR.

Signals (via ``gh``, read-only):
  * the issue's timeline cross-references / "connected" events -> linked PRs
    (the precise GitHub signal),
  * open/merged PRs whose text references the issue number (supplementary).
"""

import json
import subprocess


def _gh(args, timeout=60):
    return subprocess.run(["gh", *args], capture_output=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def _linked_prs_from_timeline(repo, num):
    """PRs linked to the issue via GitHub cross-reference/connected events."""
    prs = []
    r = _gh(["api", f"repos/{repo}/issues/{num}/timeline", "--paginate"])
    if r.returncode != 0:
        return prs, False
    try:
        events = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return prs, False
    for e in events:
        if e.get("event") not in ("cross-referenced", "connected"):
            continue
        src = (e.get("source") or {}).get("issue") or {}
        if not src.get("pull_request"):
            continue  # only PRs, not issue-to-issue references
        # Only SAME-REPO PRs are duplicate evidence. Popular repos get
        # cross-referenced from many external projects; a PR #6 in an
        # unrelated repo must not be read as this repo's PR #6 (audit
        # v2.3.6 — caddy #6293 was falsely skipped on mrdja026/…#6).
        src_repo = ((src.get("repository") or {}).get("full_name")
                    or "").lower()
        if src_repo and src_repo != repo.lower():
            continue
        prs.append({"number": src.get("number"),
                    "title": src.get("title", ""),
                    "state": (src.get("state") or "").lower(),
                    "url": src.get("html_url", ""),
                    "event": e.get("event")})
    return prs, True


def _search_prs(repo, num):
    """PRs whose title/body mentions the issue number (supplementary signal)."""
    prs = []
    r = _gh(["search", "prs", "--repo", repo, "--match", "body,title",
             str(num), "--state", "all", "--limit", "30",
             "--json", "number,title,state,url"])
    if r.returncode != 0:
        return prs
    try:
        for pr in json.loads(r.stdout or "[]"):
            if str(pr.get("number")) == str(num):
                continue
            prs.append({"number": pr.get("number"), "title": pr.get("title", ""),
                        "state": (pr.get("state") or "").lower(),
                        "url": pr.get("url", "")})
    except json.JSONDecodeError:
        pass
    return prs


def _pr_merged(repo, num):
    """Whether a PR was actually merged (timeline 'state' only says closed).
    Returns True/False, or None when the API call failed — the caller must
    treat None CONSERVATIVELY (as possibly merged), never as unmerged."""
    r = _gh(["api", f"repos/{repo}/pulls/{num}", "--jq", ".merged"])
    if r.returncode != 0:
        return None
    return r.stdout.strip() == "true"


def _pr_closes_issue(repo, pr_num, issue_num):
    """Whether the PR formally closes THIS issue (closingIssuesReferences).
    A cross-referenced event is a mere mention — PR #8805 "fixes #8363"
    mentioned #8019 in passing and got it skipped (audit). Fail-CONSERVATIVE:
    on API failure assume True (keeps the original skip behavior)."""
    owner, name = repo.split("/")
    q = ("query($o:String!,$n:String!,$pr:Int!){repository(owner:$o,name:$n)"
         "{pullRequest(number:$pr){closingIssuesReferences(first:20)"
         "{nodes{number}}}}}")
    r = _gh(["api", "graphql", "-f", f"o={owner}", "-f", f"n={name}",
             "-F", f"pr={pr_num}", "-f", f"query={q}"])
    if r.returncode != 0:
        return True
    try:
        nodes = (json.loads(r.stdout)["data"]["repository"]["pullRequest"]
                 ["closingIssuesReferences"]["nodes"])
        return any(n.get("number") == int(issue_num) for n in nodes)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return True


def find_duplicates(repo, issue_number, *, our_login=None):
    """Return a report dict:
      {is_duplicate, confidence, signals:[str], prs:[{number,title,state,url}],
       recommend}
    ``is_duplicate`` is True when an OPEN or MERGED PR is *linked* to the issue
    (strong) or an OPEN PR references it. A closed-unmerged PR is a prior
    attempt that failed or was abandoned — surfaced as a signal, but NOT
    duplicate evidence (audit: rclone#8019 / hugo#15101 were skipped on
    closed-unmerged mentions, one of which fixed a different issue entirely).
    ``our_login`` excludes our own PRs from the signal.
    """
    num = str(issue_number)
    linked, timeline_ok = _linked_prs_from_timeline(repo, num)
    searched = _search_prs(repo, num)

    by = {}
    for p in linked:
        if p.get("number"):
            by[p["number"]] = {**p, "linked": True}
    for p in searched:
        n = p.get("number")
        if n and n not in by:
            by[n] = {**p, "linked": False}
    prs = list(by.values())
    # Note: we deliberately do NOT exclude our own PRs. An existing PR by us on
    # this issue is also "already worked" and should be skipped just the same.
    # (``our_login`` is accepted for API stability but intentionally unused.)

    signals = []
    linked_prs = [p for p in prs if p.get("linked")]
    open_prs = [p for p in prs if p.get("state") == "open"]
    # Closed linked PRs only count if they actually MERGED, and (for mere
    # cross-reference mentions) formally close THIS issue — a merged PR that
    # mentioned the number while fixing a different issue is not evidence.
    # ALL API failures resolve conservatively (toward "duplicate"): a merged
    # fix we couldn't verify must skip, never proceed (audit v2.3.5 — the
    # first cut failed OPEN when _pr_merged errored).
    live_linked, dead_linked = [], []
    for p in linked_prs:
        if p.get("state") == "open":
            live_linked.append(p)
            continue
        merged = _pr_merged(repo, p.get("number"))
        if merged is None:
            live_linked.append({**p, "state": "closed (merge state unknown — "
                                              "treated as duplicate evidence)"})
        elif merged and (p.get("event") == "connected"
                         or _pr_closes_issue(repo, p.get("number"), num)):
            live_linked.append({**p, "state": "merged"})
        else:
            dead_linked.append(p)
    if live_linked:
        signals.append(f"{len(live_linked)} open/merged PR(s) linked to the issue "
                       f"via GitHub references: "
                       f"{', '.join('#'+str(p['number']) for p in live_linked)}")
    if dead_linked:
        signals.append(f"{len(dead_linked)} PR(s) mention the issue but are "
                       f"closed unmerged or fix a different issue (not "
                       f"duplicate evidence): "
                       f"{', '.join('#'+str(p['number']) for p in dead_linked)}")
    if open_prs:
        signals.append(f"{len(open_prs)} open PR(s) reference the issue")
    closed_searched = [p for p in prs
                       if not p.get("linked") and p.get("state") != "open"]
    if closed_searched:
        signals.append(f"{len(closed_searched)} closed PR(s) reference the "
                       f"issue (informational)")

    is_dup = bool(live_linked) or bool(open_prs)
    confidence = 0.9 if live_linked else (0.6 if open_prs else 0.2)
    if not timeline_ok:
        confidence = min(confidence, 0.4)
        signals.append("timeline lookup failed — duplicate signal is weaker")

    recommend = "SKIP — already being addressed" if is_dup else "proceed"
    return {"is_duplicate": is_dup, "confidence": round(confidence, 2),
            "signals": signals, "prs": prs, "recommend": recommend}
