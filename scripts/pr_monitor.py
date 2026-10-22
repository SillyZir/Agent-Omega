"""PR Monitoring & Learning — classify every maintainer/bot/CI event on a PR,
root-cause it, and feed structured knowledge back into the repository profile.

For each event: class (CI failure / maintainer review / requested changes /
approval / merge / close / duplicate / policy issue / documentation issue /
repository preference / other), root cause, category (code / pipeline /
repository requirement / maintainer preference), and the smallest architectural
improvement. Never modifies the PR.

Usage:
  python3 scripts/pr_monitor.py <owner/repo> <pr_number> [<pr_number> ...]
"""

import json
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from agent import repo_profile  # noqa: E402

_POLICY = ("contributing.md", "contributing a change", "ai usage", "ai-generated",
           "llm", "🤖", "code of conduct", "guidelines")
_DUP = ("duplicate", "already fixed", "already being", "will be fixed by",
        "superseded", "dupe of", "resolved by")
_DOCS = ("documentation", "docs", "readme", "changelog entry", "add a changelog")

# (class, category) -> smallest architectural improvement
_IMPROVEMENT = {
    ("close", "repository requirement"): "duplicate detection + repo-profile compliance gate (v2.3) before shipping",
    ("duplicate", "pipeline issue"): "pre-work duplicate detection at issue selection (v2.3)",
    ("policy issue", "repository requirement"): "encode the policy in the repo profile and satisfy it automatically (v2.3)",
    ("ci failure", "repository requirement"): "detect+generate the required artifact in the compliance stage (v2.3)",
    ("ci failure", "code issue"): "add the failing check to the local affected-test gate",
    ("requested changes", "maintainer preference"): "record the preference in the repo profile.learned and apply next time",
    ("documentation issue", "repository requirement"): "add the required doc artifact to contribution-awareness",
}


def _gh(args, timeout=60):
    return subprocess.run(["gh", *args], capture_output=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def _has(text, needles):
    t = (text or "").lower()
    return any(n in t for n in needles)


_BOT_LOGINS = ("hashicorp-cla-app", "github-actions", "dependabot",
               "cla-assistant", "claassistant", "codecov", "netlify",
               "vercel", "sonarcloud", "codecov-commenter", "codecov-io")

# Author associations that make a commenter a MAINTAINER of the repo —
# only their feedback belongs in learned maintainer_prefs (audit v2.3.5:
# our own comments and a reporter's comment were being stored as prefs).
_MAINTAINER_ASSOC = ("MEMBER", "OWNER", "COLLABORATOR")


def _is_bot(login):
    lg = (login or "").lower()
    return ("[bot]" in lg or lg.endswith("-app") or lg in _BOT_LOGINS)


def _login(obj):
    """Login of a comment/review author, None-safe (GitHub returns author=null
    for deleted/ghost accounts)."""
    return ((obj or {}).get("author") or {}).get("login") or ""


def classify_comment(body, author):
    """Classify a comment/close-reason into (class, category, root_cause)."""
    if _has(body, _DUP):
        m = re.search(r"#(\d{3,6})", body or "")
        ref = f" (points to #{m.group(1)})" if m else ""
        return ("duplicate", "pipeline issue",
                f"issue already being addressed elsewhere{ref}")
    if _has(body, _POLICY):
        return ("policy issue", "repository requirement",
                "maintainer cited contribution policy (CONTRIBUTING / AI usage)")
    if _has(body, _DOCS):
        return ("documentation issue", "repository requirement",
                "a required doc/changelog artifact is missing or inadequate")
    if author and author.endswith("[bot]"):
        return ("other", "repository requirement", "automated bot notice")
    return ("other feedback", "maintainer preference", "maintainer comment")


def classify_check(name, bucket):
    n = name.lower()
    if bucket != "fail":
        return None
    # \bcla\b avoids matching clang-*; OS/arch/version-named jobs (linux,
    # windows, mac, go1.xx — the norm for Go projects) are build+test jobs,
    # i.e. code issues (audit v2.3.5: rclone's 'linux' fell to pipeline issue).
    cat = ("repository requirement" if ("changelog" in n or "license" in n
                                        or re.search(r"\bcla\b", n))
           else "code issue" if any(k in n for k in (
               "test", "build", "lint", "check", "ci", "compile", "go",
               "linux", "windows", "mac", "android", "vet", "race"))
           else "pipeline issue")
    return ("ci failure", cat, f"required check '{name}' failed")


def monitor_pr(repo, num, *, update_profile=True):
    """Fetch a PR's lifecycle, classify events, and (optionally) fold findings
    into the repo profile. Returns {pr, state, events:[...]}."""
    view = _gh(["pr", "view", str(num), "--repo", repo, "--json",
                "state,isDraft,closed,author,comments,reviews,"
                "statusCheckRollup,title,url"])
    if view.returncode != 0:
        return {"pr": num, "error": view.stderr.strip()[:200]}
    d = json.loads(view.stdout)
    events = []
    # The PR author is US — our own comments are never maintainer signal
    # (audit v2.3.5: operator replies were learned as maintainer_prefs).
    pr_author = ((d.get("author") or {}).get("login") or "").lower()

    def _ours(obj):
        return _login(obj).lower() == pr_author if pr_author else False

    # terminal state (state is OPEN / CLOSED / MERGED)
    if d["state"] == "MERGED":
        events.append({"class": "merge", "category": "—",
                       "root_cause": "maintainer merged the contribution"})
    consumed = None  # the comment already emitted as the close attribution
    if d["state"] == "CLOSED":
        # attribute the close to the last human non-author comment, if any
        human = [c for c in d.get("comments", [])
                 if not _is_bot(_login(c)) and not _ours(c)]
        if human:
            consumed = id(human[-1])
            cls, cat, rc = classify_comment(human[-1].get("body",""), _login(human[-1]))
            events.append({"class": "close", "category": cat, "root_cause": rc,
                           "sub_class": cls, "actor": _login(human[-1]),
                           "evidence": (human[-1].get("body") or "")[:200]})
        else:
            events.append({"class": "close", "category": "maintainer preference",
                           "root_cause": "closed without an explanatory comment"})

    # reviews (skip our own — an author can't review their own PR, but be safe)
    for r in d.get("reviews", []):
        if _ours(r):
            continue
        st = r.get("state", "")
        assoc = r.get("authorAssociation", "")
        if st == "CHANGES_REQUESTED":
            events.append({"class": "requested changes", "category": "maintainer preference",
                           "root_cause": "maintainer requested changes",
                           "actor": _login(r), "assoc": assoc,
                           "evidence": (r.get("body") or "")[:200]})
        elif st == "APPROVED":
            events.append({"class": "approval", "category": "—",
                           "root_cause": "maintainer approved", "actor": _login(r),
                           "assoc": assoc})
        elif r.get("body"):
            cls, cat, rc = classify_comment(r.get("body",""), _login(r))
            events.append({"class": "maintainer review", "category": cat,
                           "root_cause": rc, "actor": _login(r), "assoc": assoc,
                           "evidence": r["body"][:200]})

    # comments (human, non-author; skip the one already used for the close)
    for c in d.get("comments", []):
        if id(c) == consumed or _is_bot(_login(c)) or _ours(c):
            continue
        author = _login(c)
        cls, cat, rc = classify_comment(c.get("body",""), author)
        events.append({"class": cls, "category": cat, "root_cause": rc,
                       "actor": author, "assoc": c.get("authorAssociation", ""),
                       "evidence": (c.get("body") or "")[:200]})

    # failing checks (only genuine failures; action_required/pending are not fails)
    for chk in d.get("statusCheckRollup", []) or []:
        concl = (chk.get("conclusion") or chk.get("state") or "").lower()
        if concl not in ("failure", "error"):
            continue
        res = classify_check(chk.get("name") or chk.get("context", ""), "fail")
        if res:
            cls, cat, rc = res
            events.append({"class": cls, "category": cat, "root_cause": rc,
                           "evidence": chk.get("name")})

    # attach the smallest improvement to each event
    for e in events:
        key = (e.get("sub_class") or e["class"], e["category"])
        e["improvement"] = _IMPROVEMENT.get(key) or _IMPROVEMENT.get(
            (e["class"], e["category"]), "none / informational")

    result = {"pr": num, "url": d.get("url"), "title": d.get("title"),
              "state": d["state"], "merged": d["state"] == "MERGED", "events": events}

    if update_profile:
        _learn(repo, num, result)
    return result


def _learn(repo, num, result):
    """Fold classified events into the repo profile's learned knowledge."""
    prof = repo_profile.load(repo)
    if not prof:
        return
    learned = prof.setdefault("learned", {"maintainer_prefs": [], "failure_modes": [], "events": []})
    seen = {e.get("id") for e in learned.get("events", [])}
    for e in result["events"]:
        eid = f"{num}:{e['class']}:{e.get('actor','')}:{(e.get('evidence') or '')[:40]}"
        if eid in seen:
            continue
        learned.setdefault("events", []).append({"id": eid, "pr": num, **{
            k: e[k] for k in ("class", "category", "root_cause", "improvement") if k in e}})
        if e["class"] in ("close", "duplicate", "policy issue", "ci failure"):
            fm = f"{e['class']}: {e['root_cause']}"
            if fm not in learned["failure_modes"]:
                learned["failure_modes"].append(fm)
        # Only actual maintainers' words become maintainer_prefs — community
        # commenters (reporters, drive-by users) are events, not preferences.
        if (e["class"] in ("requested changes", "maintainer review", "other feedback")
                and e.get("evidence")
                and e.get("assoc") in _MAINTAINER_ASSOC):
            pref = e["evidence"].strip()
            if pref and pref not in learned["maintainer_prefs"]:
                learned["maintainer_prefs"].append(pref)
    repo_profile.save(prof)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    repo = sys.argv[1]
    for num in sys.argv[2:]:
        r = monitor_pr(repo, int(num))
        print(f"\n=== {repo}#{num}  [{r.get('state')}{' MERGED' if r.get('merged') else ''}] "
              f"{r.get('title','')}")
        if r.get("error"):
            print("  error:", r["error"]); continue
        for e in r["events"]:
            print(f"  • {e['class'].upper():18} cat={e['category']:24} {e['root_cause']}")
            print(f"    -> improvement: {e['improvement']}")
            if e.get("evidence"):
                print(f"    evidence: {e['evidence'][:120].strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
