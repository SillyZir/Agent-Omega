"""Regression-test generation (v2.4 — evidence-justified freeze exception).

Given an otherwise-qualified bug fix (judge passed, affected tests pass) this
stage generates the fix's OWN targeted regression test and verifies it against
a strict acceptance invariant BEFORE it may satisfy the auto-ship test-standard
gate:

    base commit + generated test  -> FAIL (because of the reported bug)
    patched tree + the SAME test  -> PASS

Red->green alone is NOT sufficient: an INDEPENDENT, fix-blind evaluation must
also establish that the failure represents the REPORTED behavior and that the
test asserts the real behavioral contract, not a superficial implementation
detail (#7905 reference lesson). Anything flaky / ambiguous / superficial /
over-broad / environment-dependent / production-touching FAILS CLOSED: the fix
is left intact (test-less) and routed to human review; nothing is weakened.

The generation agent is BLIND to the fix (issue + buggy base tree only), so it
tests the contract rather than the fix's mechanism. On success the working tree
is left as fix+test so the pipeline ships the EXACT verified artifact; on any
failure the tree is restored to fix-only.

Reuses existing primitives — no new test-running or agent machinery:
  agentic_session.run_agentic_session / capture_diff
  test_exec.apply_patch / reset_tree / run_go_tests / go_test_names /
           test_files_of / _looks_like_compile_error / _run
  regression_harness._changed_files
  judge.judge_test_validity
"""

import hashlib
import os
import subprocess

from agent import agentic_session as ag
from agent import test_exec as te
from agent import regression_harness as rh
from agent import judge as judge_mod

TEST_GEN_TOOLS = "Read,Grep,Glob,Write"   # NO Edit / Bash -> cannot touch prod
TEST_GEN_MAX_TURNS = "35"
TEST_GEN_TIMEOUT = 900                     # 15 min
BODY_CAP = 3000

TEST_GEN_PROMPT = """You are writing ONE minimal regression test for a real, \
reported bug. Your working directory is the repository root, checked out at the \
commit where the bug still EXISTS (unfixed). You have NOT been shown any fix — \
work only from the issue and the code.

First READ the neighbouring existing test files for the affected package so you \
follow this repository's exact testing conventions (file naming, package, \
helpers, table style, assertion idioms). Then WRITE a single new test file that:

  1. Reproduces the REPORTED behaviour — it must FAIL on the current (buggy) \
code for the reason the issue describes.
  2. Asserts the real, observable behavioural CONTRACT the issue is about — NOT \
a superficial implementation detail (do not assert that some internal method \
was called; assert the actual effect the user would observe).
  3. Will PASS once the bug is correctly fixed.

Keep it minimal and focused. Do NOT modify any production/non-test file. Do NOT \
add unrelated tests. When finished, output ONLY a JSON object on the last line:
{{"done": true, "files": ["path/to/xxx_test.go"], "summary": "<one sentence>"}}
If you cannot write a sound reproducing test, make no changes and output:
{{"done": false, "files": [], "summary": "<why>"}}

ISSUE {key}: {title}

{body}
"""


def _diff_added_text(diff):
    """The added ('+') lines of a diff, minus the +++ header — i.e. the new
    test's source, for the independent evaluator to read."""
    return "\n".join(l[1:] for l in diff.splitlines()
                     if l.startswith("+") and not l.startswith("+++"))


def _restore_fix(path, fix_diff):
    """Leave the tree as fix-only (clean base + the fix applied)."""
    te.reset_tree(path)
    te.apply_patch(path, fix_diff)


def _fail(path, fix_diff, outcome, detail="", **extra):
    _restore_fix(path, fix_diff)
    return {"outcome": outcome, "detail": detail, **extra}


def generate_regression_test(path, issue, base_sha, fix_diff, changed_files,
                             lang, env):
    """Generate + verify a regression test for the fix currently in the working
    tree. Returns a dict; ``outcome == "generated"`` means the acceptance
    invariant AND the independent evaluation AND lint all passed and the tree is
    left as fix+test. Every other outcome is fail-closed (tree = fix-only)."""
    if lang != "go":
        # Only Go is wired for now; rust/other -> route to review, don't block.
        return {"outcome": "unsupported_lang", "detail": f"lang={lang}"}

    key = issue.get("issue_key") or f"{issue.get('repo')}#{issue.get('number')}"
    title = issue.get("title", "")
    body = (issue.get("body") or "")[:BODY_CAP]
    prompt = TEST_GEN_PROMPT.format(key=key, title=title, body=body)

    # ── generate on the BASE (buggy) tree, blind to the fix ──
    te.reset_tree(path)
    try:
        ag.run_agentic_session(prompt, path, TEST_GEN_TOOLS,
                               TEST_GEN_MAX_TURNS, TEST_GEN_TIMEOUT, env)
    except subprocess.TimeoutExpired:
        return _fail(path, fix_diff, "gen_timeout", "test-gen session timed out")
    except Exception as e:  # noqa: BLE001 - never let gen crash the pipeline
        return _fail(path, fix_diff, "gen_error",
                     f"{type(e).__name__}: {str(e)[:150]}")

    test_diff = ag.capture_diff(path, reset=False, env=env)
    if not test_diff.strip():
        return _fail(path, fix_diff, "no_test", "agent produced no test file")

    # ── guard: ONLY a single test file, no production code ──
    files = rh._changed_files(test_diff)
    prod = [f for f in files if f not in te.test_files_of(files)]
    if prod:
        return _fail(path, fix_diff, "production_touch",
                     f"test-gen touched non-test files: {prod}")
    if len(files) != 1:
        return _fail(path, fix_diff, "multi_file",
                     f"expected exactly one test file, got {files}")
    test_file = files[0]
    names = te.go_test_names(test_diff, [test_file])
    if not names:
        return _fail(path, fix_diff, "no_test_names",
                     "no Test functions in generated file")
    test_sha = hashlib.sha256(test_diff.encode("utf-8")).hexdigest()

    # ── differential: BASE+test must FAIL (stably), PATCHED+test must PASS ──
    # BASE: reset -> apply the SAME test bytes -> run named tests twice.
    te.reset_tree(path)
    ok, err = te.apply_patch(path, test_diff)
    if not ok:
        return _fail(path, fix_diff, "test_apply_failed_base", err)
    base_sha_run = _head(path)
    b1 = te.run_go_tests(path, [test_file], names)
    b2 = te.run_go_tests(path, [test_file], names)
    for (bp, bs, bo) in (b1, b2):
        if bp is not False:                      # True or None(no-run/timeout)
            return _fail(path, fix_diff, "base_not_failing",
                         f"pass={bp} status={bs}", base_output=bo[-1500:])
        if te._looks_like_compile_error(bo, lang):
            return _fail(path, fix_diff, "base_compile_error",
                         "generated test does not compile on base "
                         "(unrelated failure, not a reproduction)",
                         base_output=bo[-1500:])
    base_out = b1[2]

    # PATCHED: reset -> apply fix -> apply the SAME test bytes -> run twice.
    te.reset_tree(path)
    ok, err = te.apply_patch(path, fix_diff)
    if not ok:
        return _fail(path, fix_diff, "fix_apply_failed", err)
    patched_sha_run = _head(path)
    ok, err = te.apply_patch(path, test_diff)
    if not ok:
        return _fail(path, fix_diff, "test_apply_failed_patched", err)
    p1 = te.run_go_tests(path, [test_file], names)
    p2 = te.run_go_tests(path, [test_file], names)
    for (pp, ps, po) in (p1, p2):
        if pp is not True:
            return _fail(path, fix_diff, "patched_not_passing",
                         f"pass={pp} status={ps}", patched_output=po[-1500:])
    patched_out = p1[2]

    # contamination guard: base and patched runs are the same commit (uncommitted
    # apply), so distinguish by the applied diffs, and assert the test bytes were
    # identical on both sides.
    contamination = {"base_head": base_sha_run, "patched_head": patched_sha_run,
                     "test_sha256": test_sha}

    # ── independent evaluation (fix-blind, anti-superficial) ──
    v = judge_mod.judge_test_validity(title, body,
                                      _diff_added_text(test_diff), base_out)
    if v.get("verdict") != "valid":
        return _fail(path, fix_diff, "eval_" + str(v.get("verdict")),
                     v.get("reason", ""), verdict=v)

    # ── lint/static must stay green on patched+test ──
    te.reset_tree(path)
    te.apply_patch(path, fix_diff)
    te.apply_patch(path, test_diff)
    fmt = te._run(["gofmt", "-l", test_file], cwd=path, build_env=True)
    pkg = "./" + os.path.dirname(test_file) + "/"
    vet = te._run(["go", "vet", pkg], cwd=path, build_env=True)
    if fmt.returncode != 0 or (fmt.stdout or "").strip():
        return _fail(path, fix_diff, "lint_gofmt",
                     f"gofmt flagged: {(fmt.stdout or '').strip()[:200]}")
    if vet.returncode != 0:
        return _fail(path, fix_diff, "lint_vet",
                     f"go vet rc={vet.returncode}: "
                     f"{((vet.stderr or '') + (vet.stdout or '')).strip()[-300:]}")

    # SUCCESS — tree is fix+test (applied just above); ship the exact artifact.
    return {"outcome": "generated", "test_file": test_file, "names": names,
            "test_diff": test_diff, "test_sha256": test_sha, "verdict": v,
            "base_output": base_out[-1500:], "patched_output": patched_out[-1500:],
            "differential": contamination, "base_sha": base_sha}


def _head(path):
    r = te._run(["git", "rev-parse", "HEAD"], cwd=path)
    return (r.stdout or "").strip()
