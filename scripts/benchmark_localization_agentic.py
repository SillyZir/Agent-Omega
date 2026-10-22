"""Agentic localization benchmark — same 37 ground-truth issues, but the
localizer is an agentic session (claude -p with Read/Grep/Glob tools)
exploring the pre-fix repo checkout, instead of keyword search + ranking.

Scored identically to scripts/benchmark_localization.py for direct
comparison against the keyword baseline.

Usage: python3 scripts/benchmark_localization_agentic.py [--limit N]
Output: benchmark_localization_agentic_results.jsonl + summary.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.benchmark_localization import (  # noqa: E402
    prepare_checkout, score, summarize, BENCH_FILE,
)
# Agentic localization session lives in the shared module (single source of
# truth, reused by the production pipeline).
from agent.agentic_session import agentic_localize as _agentic_localize  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_FILE = os.path.join(_ROOT, "benchmark_localization_agentic_results.jsonl")
SESSION_TIMEOUT = 900  # seconds per issue
MAX_TURNS = "50"


def agentic_localize(path, issue):
    """Run an agentic exploration session in the checkout; return file list.

    Thin wrapper over the shared session, pinning the benchmark's budgets.
    """
    return _agentic_localize(path, issue["issue_key"], issue["title"],
                             issue["body"], max_turns=MAX_TURNS,
                             timeout=SESSION_TIMEOUT)


def _load_compacted():
    """Load prior results, keeping the best record per issue (a success
    supersedes an earlier error), and rewrite the file deduplicated so
    downstream readers never see stale superseded records."""
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
        summarize(list(best.values()), title="AGENTIC LOCALIZATION")
        return

    # Resume: skip issues already scored (errors/timeouts are retried)
    done = {k: r for k, r in best.items() if "error" not in r}
    issues = [i for i in issues if i["issue_key"] not in done]
    print(f"agentic localization on {len(issues)} issues "
          f"({len(done)} already done)", flush=True)

    results = list(done.values())
    with open(OUT_FILE, "a") as out:
        for i, iss in enumerate(issues):
            t0 = time.time()
            print(f"[{i+1}/{len(issues)}] {iss['issue_key']}", flush=True)
            path = prepare_checkout(iss["repo"], iss["fix_sha"])
            rec = {"issue_key": iss["issue_key"], "repo": iss["repo"],
                   "fix_files": iss["fix_files"],
                   "omega_success": iss.get("omega_success", False)}
            if not path:
                rec["error"] = "checkout_failed"
            else:
                predicted, err = agentic_localize(path, iss)
                if err and not predicted:
                    rec["error"] = err
                else:
                    # Normalize: verify predicted paths exist in checkout
                    rec["predicted"] = predicted
                    rec["predicted_exist"] = [
                        p for p in predicted
                        if os.path.isfile(os.path.join(path, p))]
                    rec["search_method"] = "agentic"
                    rec.update(score(predicted, iss["fix_files"]))
            rec["seconds"] = round(time.time() - t0, 1)
            print(f"    -> {rec.get('predicted', rec.get('error'))} "
                  f"({rec['seconds']}s)", flush=True)
            out.write(json.dumps(rec) + "\n")
            out.flush()
            results.append(rec)

    summarize(results, title="AGENTIC LOCALIZATION")


if __name__ == "__main__":
    sys.exit(main())
