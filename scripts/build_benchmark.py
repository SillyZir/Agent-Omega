"""Build the ground-truth localization benchmark from backtest results.

Takes every backtested issue with an identified human fix, fetches the
issue text and the fix commit SHA, and writes benchmark_issues.jsonl.
The benchmark runner checks out fix_sha^ (repo state before the fix) and
asks the localization stage to find the right file.
"""

import json
import os
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import subprocess
import sys

IN_FILE = os.path.join(_ROOT, "backtest_results.jsonl")
OUT_FILE = os.path.join(_ROOT, "benchmark_issues.jsonl")


def _gh_json(path, jq):
    r = subprocess.run(["gh", "api", path, "--jq", jq],
                       capture_output=True, encoding="utf-8",
                       errors="replace", timeout=30)
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def main():
    rows = [json.loads(l) for l in open(IN_FILE)]
    bench = [r for r in rows
             if r.get("state_reason") == "COMPLETED" and r.get("fix_files")]
    print(f"building benchmark from {len(bench)} issues", flush=True)

    written = 0
    with open(OUT_FILE, "w") as out:
        for r in bench:
            repo, num = r["issue_key"].rsplit("#", 1)

            title = _gh_json(f"repos/{repo}/issues/{num}", ".title") or ""
            body = _gh_json(f"repos/{repo}/issues/{num}", ".body // \"\"") or ""

            fix_sha = ""
            if r.get("closer_type") == "PullRequest" and r.get("closer_ref", "").startswith("PR#"):
                pr_num = r["closer_ref"][3:]
                fix_sha = _gh_json(f"repos/{repo}/pulls/{pr_num}",
                                   ".merge_commit_sha // \"\"") or ""
            elif r.get("closer_type") == "Commit":
                # closer_ref is a truncated oid; resolve the full sha
                fix_sha = _gh_json(f"repos/{repo}/commits/{r['closer_ref']}",
                                   ".sha // \"\"") or ""

            if not title or not fix_sha:
                print(f"  SKIP {r['issue_key']}: title={'y' if title else 'n'} "
                      f"fix_sha={'y' if fix_sha else 'n'}")
                continue

            out.write(json.dumps({
                "issue_key": r["issue_key"],
                "repo": repo,
                "number": int(num),
                "title": title,
                "body": body[:4000],
                "fix_sha": fix_sha,
                "fix_files": r["fix_files"],
                "closer_ref": r.get("closer_ref", ""),
                "omega_success": r.get("omega_success", False),
                "omega_files": r.get("omega_files", []),
            }) + "\n")
            written += 1
    print(f"wrote {written} benchmark issues to {OUT_FILE}")


if __name__ == "__main__":
    sys.exit(main())
