"""Execution / regression harness — two layers, matching what's actually
provable in each context.

OFFLINE (ground truth available — the 37-issue benchmark):
    The Stage-3 differential protocol (control must FAIL pre-fix, PASS with the
    agent patch; failures arbitrated by a positive control on the maintainer's
    own fixed tree). This needs the human's test files and fix commit, so it is
    an *evaluation* harness, not an inline production gate. It is re-exported
    here as ``validate_with_ground_truth`` and remains runnable standalone via
    ``scripts/benchmark_validate_tests.py`` — the permanent regression harness
    for the pipeline itself (run it after pipeline changes to catch quality
    regressions against ground truth).

PRODUCTION (live issue — no ground truth):
    There is no human fix to overlay, so the inline check is weaker by
    necessity: apply the agent patch and run the test package(s)/crate(s) the
    patch touches, to confirm it builds and doesn't break existing tests (and
    that any repro test the agent added passes). ``run_package_tests`` does this
    by reusing the exact same SSD-homed Go/Rust runners the benchmark uses.

Both layers share one set of runners (agent/test_exec) so build-cache placement
and toolchain handling never diverge — and importing this module pulls in NONE
of the benchmark/legacy code (the ground-truth differential is loaded lazily).
"""

# Generic, SSD-homed runners — dependency-free shared module (no benchmark /
# github_env / contribution_loop imports reach production this way).
from agent.test_exec import (
    run_go_tests, run_rust_tests, apply_patch, reset_tree,
    _looks_like_compile_error, issue_lang,
)


def validate_with_ground_truth(iss, fix_rec, lang):
    """Offline Stage-3 differential validation (control/patched/positive
    control). Needs ground truth (human test files + fix commit), so it is an
    evaluation tool, not an inline production gate — the implementation lives in
    the benchmark script and is imported lazily so production never drags in the
    benchmark/legacy stack."""
    from scripts.benchmark_validate_tests import validate_issue
    return validate_issue(iss, fix_rec, lang)


def _changed_files(diff_text):
    """Repo paths touched by a unified diff — adds, modifies, deletes AND
    renames. Parses the `diff --git a/<old> b/<new>` header, which is present in
    every case; the `+++ b/` line alone misses deletions (new side is
    /dev/null), which would drop the deleted path from staging."""
    files = []
    for line in diff_text.splitlines():
        if not line.startswith("diff --git a/"):
            continue
        rest = line[len("diff --git a/"):]
        sep = rest.find(" b/")
        if sep == -1:
            continue
        old, new = rest[:sep], rest[sep + 3:]
        for p in (old, new):
            if p and p not in files:
                files.append(p)
    return files


def run_package_tests(path, changed_files, lang, *, human_diff="",
                      test_names=None):
    """Production regression check: run the test package(s)/crate(s) that own
    the changed files, on the current (patched) tree.

    Returns ``(passed, status, output)`` exactly like the underlying runners:
      passed: True/False/None ; status: "" | "no_runnable_test" | "timeout".
    The patch must already be applied to ``path``.
    """
    go_files = [f for f in changed_files if f.endswith(".go")]
    rs_files = [f for f in changed_files if f.endswith((".rs", ".move"))]
    if lang == "go":
        if not go_files:
            return None, "no_runnable_test", ""
        return run_go_tests(path, go_files, test_names or [])
    if lang == "rust":
        if not rs_files:
            return None, "no_runnable_test", ""
        return run_rust_tests(path, rs_files, human_diff or "")
    return None, "toolchain_skipped", ""


def check_patch(path, agent_diff, lang):
    """Apply ``agent_diff`` to the checkout at ``path`` and run the affected
    package tests. Returns a dict with outcome + a one-line summary suitable
    for feeding the production reviewer judge.

    Outcomes:
      tests_pass        affected tests build and pass with the patch
      tests_fail        affected tests fail (assertion)
      build_error       patched tree / tests do not compile
      no_runnable_test  no test target maps to the changed files
      apply_failed      patch did not apply
      toolchain_skipped language toolchain not wired
    """
    changed = _changed_files(agent_diff)
    if lang in ("cpp", "other"):
        return {"outcome": "toolchain_skipped", "summary": "no toolchain", "changed": changed}

    ok, detail = apply_patch(path, agent_diff)
    if not ok:
        reset_tree(path)
        return {"outcome": "apply_failed", "summary": detail[:160], "changed": changed}

    passed, status, out = run_package_tests(path, changed, lang)
    reset_tree(path)

    if status == "no_runnable_test":
        return {"outcome": "no_runnable_test",
                "summary": "no test target for changed files", "changed": changed}
    if status == "timeout":
        return {"outcome": "test_timeout",
                "summary": "affected tests timed out", "changed": changed}
    if passed:
        return {"outcome": "tests_pass",
                "summary": "affected package tests pass", "changed": changed}
    if _looks_like_compile_error(out, lang):
        return {"outcome": "build_error",
                "summary": "patched tree fails to compile", "changed": changed}
    return {"outcome": "tests_fail",
            "summary": "affected package tests fail", "changed": changed,
            "output_tail": out[-800:]}


__all__ = [
    "validate_with_ground_truth", "run_package_tests", "check_patch",
    "issue_lang", "run_go_tests", "run_rust_tests",
]
