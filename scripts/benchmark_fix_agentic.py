"""Stage 2 benchmark — agentic fix generation vs real human fixes.

For each ground-truth issue (benchmark_issues.jsonl): check out the repo
just before the human fix, give an agentic session (claude -p with
Read/Grep/Glob/Edit/Write, no Bash) only the issue text, let it edit the
repo, capture `git diff`, and score the patch against the maintainer's
actual fix three ways:

  1. files_modified match (same score() as the localization benchmarks)
  2. hunk-region overlap (changed-line ranges intersect within ±5 lines)
  3. LLM judge verdict: equivalent | partial | different_approach | wrong

Read-only with respect to the world: nothing is submitted anywhere.

Usage: python3 scripts/benchmark_fix_agentic.py [--limit N] [--summary-only]
Output: benchmark_fix_agentic_results.jsonl
"""

import json
import os
import re
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from scripts.benchmark_localization import (  # noqa: E402
    prepare_checkout, score, BENCH_FILE, core_files,
)
# Agentic fix session lives in the shared module (single source of truth,
# reused by the production pipeline). Tool allowlist / no-Bash / turn budgets
# are pinned there.
from agent.agentic_session import agentic_fix  # noqa: E402
# The comparison judge is the calibrated Stage-2 judge, promoted to agent/judge
# (single source of truth; production reuses the same module).
from agent.judge import compare_patches  # noqa: E402

OUT_FILE = os.path.join(_ROOT, "benchmark_fix_agentic_results.jsonl")
SESSION_TIMEOUT = 1200
MAX_TURNS = "75"
JUDGE_TIMEOUT = 180
DIFF_CAP = 6000  # chars of each diff shown to the judge


def _run(cmd, cwd=None, timeout=600):
    return subprocess.run(cmd, cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def parse_diff_ranges(diff_text):
    """Unified diff -> {file: [(new_start, new_end), ...]}."""
    files = {}
    cur = None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
            files.setdefault(cur, [])
        elif line.startswith("+++ "):
            cur = None  # /dev/null (deletion) — skip new-side ranges
        elif line.startswith("@@") and cur is not None:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start = int(m.group(1))
                length = int(m.group(2)) if m.group(2) else 1
                files[cur].append((start, start + max(length, 1) - 1))
    return files


def region_overlap(agent_ranges, human_ranges, margin=5):
    """True if any changed region overlaps in any shared file."""
    for f, h_ranges in human_ranges.items():
        for a_lo, a_hi in agent_ranges.get(f, []):
            for h_lo, h_hi in h_ranges:
                if a_lo <= h_hi + margin and h_lo <= a_hi + margin:
                    return True
    return False


def fix_session(path, issue):
    """Run the agentic fix session; return (diff_text, declared, error).

    Thin wrapper over the shared session, pinning the benchmark's budgets.
    """
    return agentic_fix(path, issue["issue_key"], issue["title"],
                       issue["body"], max_turns=MAX_TURNS,
                       timeout=SESSION_TIMEOUT)


def human_diff(path, fix_sha):
    r = _run(["git", "diff", f"{fix_sha}^", fix_sha], cwd=path)
    if r.returncode != 0 or not r.stdout.strip():
        # Shallow history may have been pruned by a later fetch — re-fetch
        _run(["git", "fetch", "--depth", "2", "--quiet", "origin", fix_sha],
             cwd=path)
        r = _run(["git", "diff", f"{fix_sha}^", fix_sha], cwd=path)
    return r.stdout if r.returncode == 0 else ""


def judge(issue, agent_diff, human_diff_text):
    """Calibrated comparison judge — thin wrapper pinning benchmark caps."""
    return compare_patches(issue["title"], issue["body"], agent_diff,
                           human_diff_text, diff_cap=DIFF_CAP, body_cap=1200,
                           timeout=JUDGE_TIMEOUT)


def _load_compacted():
    best = {}
    if os.path.isfile(OUT_FILE):
        for line in open(OUT_FILE):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = r.get("issue_key")
            if not key:
                continue
            if key not in best or "error" in best[key]:
                best[key] = r
        with open(OUT_FILE, "w") as out:
            for r in best.values():
                out.write(json.dumps(r) + "\n")
    return best


def main():
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    issues = [json.loads(l) for l in open(BENCH_FILE)]
    if limit:
        issues = issues[:limit]

    best = _load_compacted()
    if "--summary-only" in sys.argv:
        summarize(list(best.values()))
        return

    done = {k: r for k, r in best.items() if "error" not in r}
    issues = [i for i in issues if i["issue_key"] not in done]
    print(f"agentic fix generation on {len(issues)} issues "
          f"({len(done)} already done)", flush=True)

    results = list(done.values())
    with open(OUT_FILE, "a") as out:
        for i, iss in enumerate(issues):
            t0 = time.time()
            print(f"[{i+1}/{len(issues)}] {iss['issue_key']}", flush=True)
            rec = {"issue_key": iss["issue_key"], "repo": iss["repo"],
                   "fix_files": iss["fix_files"]}
            path = prepare_checkout(iss["repo"], iss["fix_sha"])
            if not path:
                rec["error"] = "checkout_failed"
            else:
                agent_diff, declared, err = fix_session(path, iss)
                h_diff = human_diff(path, iss["fix_sha"])
                rec["declared"] = declared
                rec["agent_diff_chars"] = len(agent_diff)
                rec["agent_diff"] = agent_diff[:20000]
                rec["human_diff_chars"] = len(h_diff)
                rec["human_diff"] = h_diff[:20000]
                if err == "timeout" and agent_diff:
                    rec["timed_out"] = True  # scored, but flagged: partial work
                if err and not agent_diff:
                    rec["error"] = err
                elif not agent_diff:
                    rec["no_fix"] = True
                    rec["modified"] = []
                else:
                    a_ranges = parse_diff_ranges(agent_diff)
                    h_ranges = parse_diff_ranges(h_diff)
                    rec["modified"] = sorted(a_ranges.keys())
                    rec.update(score(rec["modified"], iss["fix_files"]))
                    rec["region_match"] = region_overlap(a_ranges, h_ranges)
                    if not h_diff.strip():
                        rec["ground_truth_missing"] = True
                        rec["judge"] = {"verdict": "no_ground_truth",
                                        "reason": "human diff unavailable"}
                    else:
                        rec["judge"] = judge(iss, agent_diff, h_diff)
            rec["seconds"] = round(time.time() - t0, 1)
            print(f"    -> modified={rec.get('modified', rec.get('error'))} "
                  f"region={rec.get('region_match')} "
                  f"verdict={rec.get('judge', {}).get('verdict')} "
                  f"({rec['seconds']}s)", flush=True)
            out.write(json.dumps(rec) + "\n")
            out.flush()
            results.append(rec)

    summarize(results)


def _is_go(rec):
    return any(f.endswith(".go") for f in core_files(rec.get("fix_files", [])))


def summarize(results):
    ok = [r for r in results if "error" not in r]
    errs = [r for r in results if "error" in r]
    fixed = [r for r in ok if not r.get("no_fix")]
    print("\n" + "=" * 64)
    print(f"AGENTIC FIX GENERATION  ({len(ok)} scored, "
          f"{sum(1 for r in ok if r.get('no_fix'))} declined, {len(errs)} errors)")
    print("=" * 64)
    for r in errs:
        print(f"  ERROR {r['issue_key']}: {r['error']}")

    def _any_exact(r):
        """Patch-level file match: does the patch modify any fix file?
        (top1_* sorts modified files alphabetically, which undercounts
        multi-file patches — this is the honest metric.)"""
        return bool(set(r.get("modified", [])) & set(r.get("fix_files", [])))

    def _any_core(r):
        return bool(set(r.get("modified", []))
                    & set(core_files(r.get("fix_files", []))))

    def block(label, rs):
        if not rs:
            return
        n = len(rs)
        print(f"\n  {label} (n={n}):")
        for name, fn in (("any_exact", _any_exact), ("any_core", _any_core)):
            c = sum(1 for r in rs if fn(r))
            print(f"    {name:14s} {c:2d}/{n}  ({c/n:.0%})")
        for metric in ("top1_exact", "region_match"):
            c = sum(1 for r in rs if r.get(metric))
            print(f"    {metric:14s} {c:2d}/{n}  ({c/n:.0%})")
        verdicts = {}
        for r in rs:
            v = r.get("judge", {}).get("verdict", "?")
            verdicts[v] = verdicts.get(v, 0) + 1
        print(f"    judge: {dict(sorted(verdicts.items(), key=lambda x: -x[1]))}")
        flagged = sum(1 for r in rs if r.get("timed_out")
                      or r.get("ground_truth_missing"))
        if flagged:
            print(f"    flagged (timed_out/no_ground_truth): {flagged}")

    block("ALL PATCHES", fixed)
    block("GO", [r for r in fixed if _is_go(r)])
    block("NON-GO", [r for r in fixed if not _is_go(r)])


if __name__ == "__main__":
    sys.exit(main())
