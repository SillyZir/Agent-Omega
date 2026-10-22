"""Backtest Omega's attempts against ground truth on GitHub.

For every issue Omega attempted, query GitHub for what actually happened:
  - issue state (open / closed-completed / closed-not_planned)
  - the PR or commit that closed it, and which files that real fix touched
Then compare the real fix's files against the files Omega changed.

No PRs are submitted; this is read-only evaluation.

Output: backtest_results.jsonl (one record per issue) + summary to stdout.
"""

import json
import os
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import subprocess
import sys
import time

MEMORY_FILE = os.path.join(_ROOT, "agent_memory.json")
EPISODES_FILE = os.path.join(_ROOT, "experience_episodes.jsonl")
OUT_FILE = os.path.join(_ROOT, "backtest_results.jsonl")

_GRAPHQL = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      state
      stateReason
      closedAt
      timelineItems(itemTypes: [CLOSED_EVENT], last: 3) {
        nodes {
          ... on ClosedEvent {
            closer {
              __typename
              ... on PullRequest {
                number
                merged
                files(first: 100) { nodes { path } }
              }
              ... on Commit { oid }
            }
          }
        }
      }
    }
  }
}
"""


def _gh_graphql(owner, name, number):
    cmd = ["gh", "api", "graphql",
           "-f", f"query={_GRAPHQL}",
           "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}"]
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                       errors="replace", timeout=30)
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip()[:200]}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"error": "bad json"}


def _commit_files(repo, sha):
    r = subprocess.run(["gh", "api", f"repos/{repo}/commits/{sha}",
                        "--jq", "[.files[].filename]"],
                       capture_output=True, encoding="utf-8",
                       errors="replace", timeout=30)
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return []


def collect_attempts():
    """Merge per-issue data from memory + episodes: files Omega changed, outcome."""
    mem = json.load(open(MEMORY_FILE))
    issues = {}

    def entry(key):
        return issues.setdefault(key, {
            "issue_key": key, "omega_success": False,
            "attempts": 0, "omega_files": set(),
        })

    for p in mem.get("successful_patterns", []):
        key = p.get("issue_key")
        if not key or "#" not in key:
            continue
        e = entry(key)
        e["omega_success"] = True
        e["omega_files"].update(p.get("changed_files") or [])

    for p in mem.get("failure_patterns", []):
        key = p.get("issue_key")
        if not key or "#" not in key:
            continue
        e = entry(key)
        e["omega_files"].update(p.get("changed_files") or [])

    for key, st in mem.get("issue_stats", {}).items():
        if "#" not in key:
            continue
        e = entry(key)
        e["attempts"] = max(e["attempts"], st.get("attempts", 0))
        if st.get("successes", 0) > 0:
            e["omega_success"] = True

    try:
        for line in open(EPISODES_FILE):
            ep = json.loads(line)
            key = f"{ep.get('repo')}#{ep.get('issue')}"
            if "#" not in key or not ep.get("repo"):
                continue
            e = entry(key)
            if ep.get("success"):
                e["omega_success"] = True
            if ep.get("file"):
                e["omega_files"].add(ep["file"])
    except OSError:
        pass

    return issues


def file_overlap(omega_files, fix_files):
    """Classify how Omega's file choice compares to the real fix."""
    if not omega_files or not fix_files:
        return "unknown"
    o = set(omega_files)
    f = set(fix_files)
    if o & f:
        return "exact_file"
    o_dirs = {"/".join(p.split("/")[:-1]) for p in o}
    f_dirs = {"/".join(p.split("/")[:-1]) for p in f}
    if o_dirs & f_dirs:
        return "same_directory"
    return "different"


def main():
    issues = collect_attempts()
    print(f"backtesting {len(issues)} issues "
          f"({sum(1 for v in issues.values() if v['omega_success'])} with Omega 'success')",
          flush=True)

    results = []
    with open(OUT_FILE, "w") as out:
        for i, (key, info) in enumerate(sorted(issues.items())):
            repo, num = key.rsplit("#", 1)
            if "/" not in repo or not num.isdigit():
                continue
            owner, name = repo.split("/", 1)
            rec = {
                "issue_key": key,
                "omega_success": info["omega_success"],
                "attempts": info["attempts"],
                "omega_files": sorted(info["omega_files"]),
            }
            data = _gh_graphql(owner, name, int(num))
            iss = (data.get("data") or {}).get("repository", {}) or {}
            iss = iss.get("issue")
            if not iss:
                rec["state"] = "error"
                rec["error"] = data.get("error", "issue not found (may be a PR)")
            else:
                rec["state"] = iss.get("state", "")
                rec["state_reason"] = iss.get("stateReason") or ""
                rec["closed_at"] = iss.get("closedAt") or ""
                fix_files, closer_type, closer_ref = [], "", ""
                for node in (iss.get("timelineItems", {}).get("nodes") or []):
                    closer = node.get("closer") if node else None
                    if not closer:
                        continue
                    closer_type = closer.get("__typename", "")
                    if closer_type == "PullRequest":
                        closer_ref = f"PR#{closer.get('number')}"
                        fix_files = [n["path"] for n in
                                     (closer.get("files", {}).get("nodes") or [])]
                    elif closer_type == "Commit":
                        closer_ref = closer.get("oid", "")[:10]
                        fix_files = _commit_files(repo, closer.get("oid", ""))
                rec["closer_type"] = closer_type
                rec["closer_ref"] = closer_ref
                rec["fix_files"] = fix_files
                rec["file_overlap"] = file_overlap(rec["omega_files"], fix_files)
            out.write(json.dumps(rec) + "\n")
            results.append(rec)
            if (i + 1) % 25 == 0:
                print(f"  {i+1}/{len(issues)} queried", flush=True)
            time.sleep(0.3)

    summarize(results)


def summarize(results):
    ok = [r for r in results if r.get("state") != "error"]
    errs = len(results) - len(ok)
    suc = [r for r in ok if r["omega_success"]]
    fail = [r for r in ok if not r["omega_success"]]

    def dist(rs):
        d = {}
        for r in rs:
            k = r["state"] if r["state"] == "OPEN" else \
                f"CLOSED/{r.get('state_reason') or '?'}"
            d[k] = d.get(k, 0) + 1
        return dict(sorted(d.items(), key=lambda x: -x[1]))

    print("\n" + "=" * 64)
    print(f"BACKTEST SUMMARY  ({len(ok)} issues resolved, {errs} errors)")
    print("=" * 64)
    print(f"\nOmega 'successes' ({len(suc)} issues):")
    print(f"  issue state now: {dist(suc)}")
    fixed = [r for r in suc if r.get("state_reason") == "COMPLETED"
             and r.get("fix_files")]
    print(f"  human fix identified: {len(fixed)}")
    for bucket in ("exact_file", "same_directory", "different"):
        n = sum(1 for r in fixed if r.get("file_overlap") == bucket)
        print(f"    {bucket:16s} {n}")
    print(f"\nOmega failures/attempts ({len(fail)} issues):")
    print(f"  issue state now: {dist(fail)}")
    ffixed = [r for r in fail if r.get("state_reason") == "COMPLETED"
              and r.get("fix_files")]
    print(f"  human fix identified: {len(ffixed)} (missed opportunities)")
    for bucket in ("exact_file", "same_directory", "different"):
        n = sum(1 for r in ffixed if r.get("file_overlap") == bucket)
        print(f"    {bucket:16s} {n}")


if __name__ == "__main__":
    sys.exit(main())
