"""SSD-homed test-execution primitives — shared by the production regression
harness and the offline Stage-3 differential.

These are generic: clone-agnostic Go/Rust test runners, patch apply/overlay,
tree reset, language detection, and compile-error sniffing. They depend ONLY on
the standard library and ``runner.env_guard`` (the build-cache single source of
truth) — deliberately NOT on any benchmark or legacy module, so importing the
production pipeline never drags in github_env / contribution_loop / the
benchmark scripts.

Extracted verbatim from the Stage-3 validator so behavior is identical.
"""

import os
import re
import subprocess

from runner.env_guard import build_env, cargo_target_dir

GO_RUN_TIMEOUT = 2400      # compile + module downloads + 600s test budget
RUST_RUN_TIMEOUT = 7200    # one cold build of a big workspace
OUTPUT_TAIL = 2500

_TEST_PATTERNS = ("_test.", ".test.", "/tests/", "/test/", "test_",
                  "/testdata/", ".exp")

# Root-level test directories have no leading path component, so the "/tests/"
# forms above cannot match them. Invisible in Go — `_test.` matches a Go test
# file regardless of directory — but Cargo integration tests (`tests/foo.rs` in
# a root package) and C++/py functional suites (`test/functional/…`) are
# identified by directory ALONE. Whole-segment equality, not a substring, so
# `latest/` and `contests/` are still not test paths.
_TEST_DIR_SEGMENTS = frozenset(("test", "tests", "testdata"))

# Dedicated build caches on the external SSD (see runner/env_guard): the default
# ~/go/pkg/mod is corrupted, and pinned rust toolchains auto-download per repo
# and must stay off the near-full boot disk.
_BUILD_ENV = build_env()


def _run(cmd, cwd=None, timeout=600, build_env=False, extra_env=None):
    env = None
    if build_env or extra_env:
        env = {**_BUILD_ENV, **(extra_env or {})}
    return subprocess.run(cmd, cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          env=env)


def issue_lang(fix_files):
    if any(f.endswith(".go") for f in fix_files):
        return "go"
    if any(f.endswith(".rs") for f in fix_files):
        return "rust"
    if any(f.endswith((".cpp", ".h", ".cc")) for f in fix_files):
        return "cpp"
    return "other"


def _is_test_path(f):
    p = f.lower().replace("\\", "/")
    if any(pat in p for pat in _TEST_PATTERNS):
        return True
    return "/" in p and p.split("/")[0] in _TEST_DIR_SEGMENTS


def test_files_of(fix_files):
    return [f for f in fix_files if _is_test_path(f)]


def go_test_names(h_diff, test_files):
    """Test functions added/modified by the human fix's test files."""
    names = set()
    in_test_file = False
    for line in h_diff.splitlines():
        if line.startswith("+++ b/"):
            in_test_file = line[6:] in test_files
        elif in_test_file and line.startswith("+"):
            m = re.match(r"\+func (Test\w+)", line)
            if m:
                names.add(m.group(1))
    return sorted(names)


def run_go_tests(path, test_files, test_names):
    """Run the packages containing the given test files."""
    pkgs = sorted({"./" + os.path.dirname(f) + "/" for f in test_files
                   if f.endswith(".go")})
    if not pkgs:
        return None, "no_runnable_test", ""
    cmd = ["go", "test", "-count=1", "-timeout", "600s"]
    if test_names:
        cmd += ["-run", "^(" + "|".join(test_names) + ")$"]
    cmd += pkgs
    try:
        r = _run(cmd, cwd=path, timeout=GO_RUN_TIMEOUT, build_env=True)
    except subprocess.TimeoutExpired:
        return None, "timeout", ""
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0:
        # `go test` exits 0 for packages with no test files ("? pkg [no test
        # files]") AND for -run filters that match nothing ("ok pkg 0.001s
        # [no tests to run]"). If NOTHING actually ran, a green result is
        # meaningless — report no_runnable_test instead of a false pass (the
        # compile check still happened, but nobody must hear "tests pass").
        ran = any(ln.startswith("ok ") and "[no tests to run]" not in ln
                  for ln in out.splitlines())
        if not ran:
            return None, "no_runnable_test", out[-OUTPUT_TAIL:]
    return r.returncode == 0, "", out[-OUTPUT_TAIL:]


def _nearest_crate(path, test_file):
    d = os.path.dirname(test_file)
    while d:
        if os.path.isfile(os.path.join(path, d, "Cargo.toml")):
            return d
        d = os.path.dirname(d)
    return ""


def run_rust_tests(path, test_files, h_diff):
    """Run the crate(s) owning the given test files."""
    rs_tests = [f for f in test_files if f.endswith((".rs", ".move", ".exp"))]
    if not rs_tests:
        return None, "no_runnable_test", ""
    crate = _nearest_crate(path, rs_tests[0])
    if not crate:
        return None, "no_runnable_test", ""
    cmd = ["cargo", "test", "--manifest-path",
           os.path.join(crate, "Cargo.toml")]
    # Keep build artifacts OUTSIDE the clone: reset_tree/git clean would
    # otherwise delete target/ between control and patched runs, forcing
    # a cold multi-GB rebuild every time (cause of the first-pass timeouts)
    repo_id = os.path.basename(path.rstrip("/"))
    target_env = {"CARGO_TARGET_DIR": cargo_target_dir(repo_id)}
    # Integration test file tests/<name>.rs -> --test <name>
    m = re.match(rf"^{re.escape(crate)}/tests/([^/]+)\.rs$", rs_tests[0])
    if m:
        cmd += ["--test", m.group(1)]
    else:
        fn_names = sorted(set(re.findall(r"\+\s*(?:async )?fn (test_\w+)",
                                         h_diff)))[:3]
        if fn_names:
            cmd += ["--"] + fn_names
    try:
        r = _run(cmd, cwd=path, timeout=RUST_RUN_TIMEOUT, build_env=True,
                 extra_env=target_env)
    except subprocess.TimeoutExpired:
        return None, "timeout", ""
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, "", out[-OUTPUT_TAIL:]


def overlay_tests(path, fix_sha, test_files):
    r = _run(["git", "checkout", fix_sha, "--"] + test_files, cwd=path)
    # exFAT spawns AppleDouble ._* companions on write; ._foo.rs in a
    # tests/ dir can confuse cargo's test discovery — purge before running
    _run(["find", path, "-name", "._*", "-delete"])
    return r.returncode == 0, (r.stderr or "")[:300]


def apply_patch(path, diff_text):
    tmp = os.path.join(path, ".agent_patch.diff")
    with open(tmp, "w") as f:
        f.write(diff_text)
    # Stage-2 captures on the exFAT volume staged AppleDouble ._* files as
    # contentless binary entries; exclude them or git apply rejects all
    r = _run(["git", "apply", "--whitespace=nowarn",
              "--exclude=._*", "--exclude=*/._*", ".agent_patch.diff"],
             cwd=path)
    os.unlink(tmp)
    return r.returncode == 0, (r.stderr or "")[:300]


def reset_tree(path):
    # reset --hard, not checkout -f .: overlaying tests via
    # `git checkout <sha> -- <file>` updates the index, so checkout -f .
    # would restore the overlaid version and leak state across runs
    _run(["git", "reset", "--hard", "-q"], cwd=path)
    _run(["git", "clean", "-fdq"], cwd=path)


def _looks_like_compile_error(output, lang):
    if lang == "go":
        return ("--- FAIL:" not in output and
                ("build failed" in output or "# " in output
                 or "syntax error" in output or "undefined:" in output
                 or "cannot find" in output))
    return ("error[E" in output or "could not compile" in output
            or ("test result:" not in output and "error:" in output))
