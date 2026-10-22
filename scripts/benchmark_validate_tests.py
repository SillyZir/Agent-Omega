"""Stage 3 — execution validation of agent patches against the
maintainer's own regression tests.

Differential protocol per issue:
  CONTROL: pre-fix checkout + human fix's test files overlaid -> tests
           must FAIL (proves the test exercises the bug)
  PATCHED: agent patch applied + same tests overlaid -> tests pass?

Outcomes:
  validated            control fail -> patched pass
  not_fixed_assert     patched run fails with test assertions
  not_fixed_compile    patched run fails to build (human test may
                       reference symbols only the human fix introduced)
  non_discriminating   control already passes (test doesn't catch bug)
  control_build_error  repo/test doesn't build even unpatched
  apply_failed         stored agent diff doesn't apply
  no_regression_test   human fix shipped no test files
  no_runnable_test     test files exist but no runnable harness mapping
  toolchain_skipped    language toolchain not available/approved
  test_timeout_*       run exceeded budget

Usage:
  python3 scripts/benchmark_validate_tests.py [--lang go|rust|all]
      [--limit N] [--only issue_key] [--summary-only]
Output: benchmark_validation_results.jsonl
"""

import json
import os
import shutil
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from scripts.benchmark_localization import prepare_checkout, BENCH_FILE  # noqa: E402
from scripts.benchmark_fix_agentic import (  # noqa: E402
    human_diff, OUT_FILE as FIX_RESULTS_FILE,
)
# Generic, SSD-homed test-execution primitives live in agent/test_exec — the
# single source of truth shared with the production regression harness.
from agent.test_exec import (  # noqa: E402
    _run, issue_lang, test_files_of, go_test_names, run_go_tests,
    run_rust_tests, overlay_tests, apply_patch, reset_tree,
    _looks_like_compile_error,
)

OUT_FILE = os.path.join(_ROOT, "benchmark_validation_results.jsonl")


def load_compacted(path):
    best = {}
    if os.path.isfile(path):
        for line in open(path):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = r.get("issue_key")
            if not key:
                continue
            # Last record wins: records are appended chronologically, so
            # the latest reflects the newest protocol/retry
            best[key] = r
    return best


def validate_issue(iss, fix_rec, lang):
    rec = {"issue_key": iss["issue_key"], "repo": iss["repo"], "lang": lang,
           "judge_verdict": fix_rec.get("judge", {}).get("verdict", "?")}
    tests = test_files_of(iss["fix_files"])
    rec["test_files"] = tests
    if not tests:
        rec["outcome"] = "no_regression_test"
        return rec
    agent_diff = fix_rec.get("agent_diff", "")
    if not agent_diff or fix_rec.get("agent_diff_chars", 0) > len(agent_diff):
        rec["outcome"] = "apply_failed"
        rec["detail"] = "stored diff missing or truncated"
        return rec

    path = prepare_checkout(iss["repo"], iss["fix_sha"])
    if not path:
        rec["outcome"] = "checkout_failed"
        return rec
    h_diff = human_diff(path, iss["fix_sha"])
    test_names = go_test_names(h_diff, tests) if lang == "go" else []
    rec["test_names"] = test_names

    runner = run_go_tests if lang == "go" else run_rust_tests

    # ── CONTROL: unpatched tree + human tests ──
    ok, e1 = overlay_tests(path, iss["fix_sha"], tests)
    if not ok:
        rec["outcome"] = "control_build_error"
        rec["detail"] = "test overlay failed: " + e1
        reset_tree(path)
        return rec
    if lang == "go":
        passed, err, out = runner(path, tests, test_names)
    else:
        passed, err, out = runner(path, tests, h_diff)
    rec["control_output"] = out
    reset_tree(path)
    if err == "timeout":
        rec["outcome"] = "test_timeout_control"
        return rec
    if err == "no_runnable_test":
        rec["outcome"] = "no_runnable_test"
        return rec
    if passed:
        rec["outcome"] = "non_discriminating"
        return rec
    if _looks_like_compile_error(out, lang):
        rec["outcome"] = "control_build_error"
        return rec

    # ── PATCHED: agent patch + human tests ──
    ok, e2 = apply_patch(path, agent_diff)
    if not ok:
        rec["outcome"] = "apply_failed"
        rec["detail"] = e2
        reset_tree(path)
        return rec
    ok, e3 = overlay_tests(path, iss["fix_sha"], tests)
    if not ok:
        rec["outcome"] = "control_build_error"
        rec["detail"] = "overlay after patch failed: " + e3
        reset_tree(path)
        return rec
    if lang == "go":
        passed, err, out = runner(path, tests, test_names)
    else:
        passed, err, out = runner(path, tests, h_diff)
    rec["patched_output"] = out
    reset_tree(path)
    if err == "timeout":
        rec["outcome"] = "test_timeout_patched"
    elif passed:
        rec["outcome"] = "validated"
    elif _looks_like_compile_error(out, lang):
        rec["outcome"] = "not_fixed_compile"
    else:
        rec["outcome"] = "not_fixed_assert"

    # ── POSITIVE CONTROL for failures: does the test pass on the
    # maintainer's own fixed tree? If not, the failure is an environment
    # artifact (network, missing fixtures), not a verdict on the patch.
    if rec["outcome"].startswith("not_fixed"):
        _run(["git", "checkout", "-f", "--quiet", iss["fix_sha"]], cwd=path)
        if lang == "go":
            h_passed, h_err, h_out = runner(path, tests, test_names)
        else:
            h_passed, h_err, h_out = runner(path, tests, h_diff)
        rec["human_control_output"] = h_out
        if h_err == "timeout":
            rec["human_control"] = "timeout"
        elif h_passed:
            rec["human_control"] = "pass"  # test is sound; patch truly failed
        else:
            rec["human_control"] = "fail"
            rec["outcome"] = "env_cannot_run"
        _run(["git", "checkout", "-f", "--quiet", f"{iss['fix_sha']}^"],
             cwd=path)
        reset_tree(path)
    return rec


def main():
    args = sys.argv[1:]
    lang_filter = args[args.index("--lang") + 1] if "--lang" in args else "go"
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
    only = args[args.index("--only") + 1] if "--only" in args else ""

    best = load_compacted(OUT_FILE)
    if best:
        with open(OUT_FILE, "w") as out:
            for r in best.values():
                out.write(json.dumps(r) + "\n")
    if "--summary-only" in args:
        summarize(list(best.values()))
        return

    fix_results = load_compacted(FIX_RESULTS_FILE)
    bench = {json.loads(l)["issue_key"]: json.loads(l)
             for l in open(BENCH_FILE)}

    todo = []
    for key, fr in sorted(fix_results.items()):
        if "error" in fr or fr.get("no_fix"):
            continue
        iss = bench.get(key)
        if not iss:
            continue
        lang = issue_lang(iss["fix_files"])
        if only and key != only:
            continue
        if not only:
            if lang_filter != "all" and lang != lang_filter:
                continue
            done = best.get(key)
            if done:
                outcome = done.get("outcome", "")
                retryable = outcome in ("checkout_failed",
                                        "test_timeout_control",
                                        "test_timeout_patched")
                # not_fixed results from before the positive-control arm
                # need re-validation to rule out environment artifacts
                needs_human_control = (outcome.startswith("not_fixed")
                                       and "human_control" not in done)
                if not retryable and not needs_human_control:
                    continue
        todo.append((iss, fr, lang))
    if limit:
        todo = todo[:limit]

    print(f"validating {len(todo)} issues (lang={lang_filter})", flush=True)
    results = list(best.values())
    with open(OUT_FILE, "a") as out:
        for i, (iss, fr, lang) in enumerate(todo):
            t0 = time.time()
            print(f"[{i+1}/{len(todo)}] {iss['issue_key']} ({lang})",
                  flush=True)
            if lang in ("cpp", "other"):
                rec = {"issue_key": iss["issue_key"], "repo": iss["repo"],
                       "lang": lang, "outcome": "toolchain_skipped",
                       "judge_verdict": fr.get("judge", {}).get("verdict", "?")}
            elif lang == "rust" and not shutil.which("cargo"):
                rec = {"issue_key": iss["issue_key"], "repo": iss["repo"],
                       "lang": lang, "outcome": "toolchain_skipped",
                       "judge_verdict": fr.get("judge", {}).get("verdict", "?")}
            else:
                rec = validate_issue(iss, fr, lang)
            rec["seconds"] = round(time.time() - t0, 1)
            print(f"    -> {rec['outcome']} ({rec['seconds']}s)", flush=True)
            out.write(json.dumps(rec) + "\n")
            out.flush()
            results.append(rec)
    summarize(results)


def summarize(results):
    best = {}
    for r in results:
        best[r["issue_key"]] = r
    rs = list(best.values())
    print("\n" + "=" * 64)
    print(f"STAGE 3 — EXECUTION VALIDATION  ({len(rs)} issues)")
    print("=" * 64)

    def block(label, group):
        if not group:
            return
        buckets = {}
        for r in group:
            buckets[r["outcome"]] = buckets.get(r["outcome"], 0) + 1
        print(f"\n  {label} (n={len(group)}):")
        for k, v in sorted(buckets.items(), key=lambda x: -x[1]):
            print(f"    {k:22s} {v}")
        decisive = [r for r in group if r["outcome"] in
                    ("validated", "not_fixed_assert", "not_fixed_compile")]
        v = sum(1 for r in decisive if r["outcome"] == "validated")
        if decisive:
            print(f"    -- validated: {v}/{len(decisive)} of decisive runs")

    block("ALL", rs)
    for lang in ("go", "rust"):
        block(lang.upper(), [r for r in rs if r.get("lang") == lang])

    print("\n  judge verdict vs execution (decisive runs):")
    for verdict in ("equivalent", "different_approach", "partial", "wrong"):
        group = [r for r in rs if r.get("judge_verdict") == verdict
                 and r["outcome"] in ("validated", "not_fixed_assert",
                                      "not_fixed_compile")]
        if group:
            v = sum(1 for r in group if r["outcome"] == "validated")
            print(f"    {verdict:20s} {v}/{len(group)} validated")


if __name__ == "__main__":
    sys.exit(main())
