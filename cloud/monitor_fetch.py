"""Read-only PR state fetcher for the cloud monitoring routine.

Tries authenticated `gh` first; on failure falls back to the unauthenticated
public GitHub REST API (public repositories only — private repos 404 there).
Never writes anything: state updates remain the caller's decision, so the
routine's fail-honest rule (leave cached state unchanged when a PR can't be
fetched) is enforced by construction.

Output (stdout, one JSON object):
  {"mode": "authenticated" | "public_rest_fallback" | "unreachable",
   "prs":    [{"repo", "number", "source": "gh"|"public_rest", ...gh-shaped fields}],
   "errors": [{"repo", "number", "error"}]}

mode is "authenticated" when every PR came via gh, "public_rest_fallback" when
any PR needed the public API, "unreachable" when nothing could be fetched.
PRs listed in "errors" were NOT fetched — keep their cached state.

In fallback mode, reviewDecision is derived from the reviews endpoint (latest
APPROVED/CHANGES_REQUESTED per reviewer, dismissals honored); an empty value
means "no decision derivable" and CANNOT be distinguished from REVIEW_REQUIRED.
Checks merge /check-runs with the legacy /status endpoint — legacy contexts
(e.g. license/cla) never appear in check-runs.

Usage:
  python3 cloud/monitor_fetch.py [--targets cloud/monitor_targets.json] [--force-public]
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
_GH_FIELDS = "state,isDraft,reviewDecision,comments,reviews,statusCheckRollup,updatedAt"
_LEGACY_CONCLUSION = {"success": "SUCCESS", "failure": "FAILURE",
                      "error": "ERROR", "pending": "PENDING"}


def _gh_fetch(repo, num):
    p = subprocess.run(["gh", "pr", "view", str(num), "--repo", repo,
                        "--json", _GH_FIELDS],
                       capture_output=True, encoding="utf-8", errors="replace",
                       timeout=60)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "gh failed").strip()[:200])
    d = json.loads(p.stdout)
    d["reviewDecisionSource"] = "graphql"
    return d


def _rest(path):
    req = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "omega-cloud-monitor"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _classify(e):
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (403, 429):
            return f"rate_limited_or_forbidden (HTTP {e.code})"
        if e.code == 404:
            return "not_found_or_private (fallback is public-repos-only)"
        return f"http_error (HTTP {e.code})"
    if isinstance(e, urllib.error.URLError):
        return f"network_unreachable ({getattr(e, 'reason', e)})"
    return f"{type(e).__name__}: {e}"


def _derive_review_decision(reviews):
    """Latest decision-bearing review per reviewer; any CHANGES_REQUESTED wins.
    Empty string = not derivable (REVIEW_REQUIRED vs no-reviews is GraphQL-only)."""
    latest = {}
    for r in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        login = ((r.get("user") or {}).get("login") or "").lower()
        if not login:
            continue
        st = r.get("state")
        if st in ("APPROVED", "CHANGES_REQUESTED"):
            latest[login] = st
        elif st == "DISMISSED":
            latest.pop(login, None)
    if "CHANGES_REQUESTED" in latest.values():
        return "CHANGES_REQUESTED"
    if "APPROVED" in latest.values():
        return "APPROVED"
    return ""


def _public_fetch(repo, num):
    pr = _rest(f"/repos/{repo}/pulls/{num}")
    reviews = _rest(f"/repos/{repo}/pulls/{num}/reviews?per_page=100")
    comments = _rest(f"/repos/{repo}/issues/{num}/comments?per_page=100")
    sha = pr["head"]["sha"]
    checks = _rest(f"/repos/{repo}/commits/{sha}/check-runs?per_page=100").get("check_runs", [])
    legacy = _rest(f"/repos/{repo}/commits/{sha}/status").get("statuses", [])
    return {
        "state": "MERGED" if pr.get("merged_at") else pr["state"].upper(),
        "isDraft": bool(pr.get("draft")),
        "reviewDecision": _derive_review_decision(reviews),
        "reviewDecisionSource": "derived_latest_review_per_reviewer",
        "updatedAt": pr.get("updated_at"),
        "comments": [{"author": {"login": ((c.get("user") or {}).get("login") or "")},
                      "authorAssociation": c.get("author_association", ""),
                      "body": c.get("body") or "",
                      "createdAt": c.get("created_at")} for c in comments],
        "reviews": [{"author": {"login": ((r.get("user") or {}).get("login") or "")},
                     "authorAssociation": r.get("author_association", ""),
                     "state": r.get("state"), "body": r.get("body") or "",
                     "submittedAt": r.get("submitted_at")} for r in reviews],
        "statusCheckRollup":
            [{"name": c.get("name"), "status": (c.get("status") or "").upper(),
              "conclusion": (c.get("conclusion") or "").upper()} for c in checks] +
            [{"name": s.get("context"), "status": "COMPLETED",
              "conclusion": _LEGACY_CONCLUSION.get(s.get("state"),
                                                   (s.get("state") or "").upper())}
             for s in legacy],
    }


def main(argv):
    targets_path = "cloud/monitor_targets.json"
    force_public = "--force-public" in argv
    if "--targets" in argv:
        targets_path = argv[argv.index("--targets") + 1]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.isabs(targets_path):
        targets_path = os.path.join(root, targets_path)
    targets = json.load(open(targets_path))["prs"]

    prs, errors, used_rest = [], [], False
    for t in targets:
        repo, num = t["repo"], t["number"]
        data, src, gh_err = None, None, None
        if not force_public:
            try:
                data, src = _gh_fetch(repo, num), "gh"
            except Exception as e:
                gh_err = str(e)
        if data is None:
            try:
                data, src = _public_fetch(repo, num), "public_rest"
                used_rest = True
            except Exception as e:
                prefix = f"gh: {gh_err} | " if gh_err else ""
                errors.append({"repo": repo, "number": num,
                               "error": f"{prefix}public_rest: {_classify(e)}"})
                continue
        prs.append({"repo": repo, "number": num, "source": src, **data})

    mode = ("unreachable" if not prs else
            "public_rest_fallback" if used_rest else "authenticated")
    print(json.dumps({"mode": mode, "prs": prs, "errors": errors}, indent=1))
    return 0 if prs or not targets else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
