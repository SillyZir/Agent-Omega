"""Shared agentic localize+fix session — the v2 architecture's core capability.

This is the single implementation of the constrained ``claude -p`` session
that Stages 1–3 proved: a tool-using agent that explores a real checkout and
either names the file(s) to change (localization) or edits the repo to fix the
bug (fix generation). It is deliberately constrained:

  * explicit tool allowlist, **no Bash** — the agent reasons over code, it does
    not run builds or shell out;
  * the issue text is the only context it is given;
  * a turn budget and a wall-clock timeout bound each session.

These constraints are part of the contract that produced the benchmark numbers
(localization 78.4% top-1 and 89.2% top-3 on 37 issues; patches touch a
human-fix file 75.7% of the time) — do not relax them.

Both the benchmark scripts (``scripts/benchmark_fix_agentic.py``,
``scripts/benchmark_localization_agentic.py``) and the production pipeline
(``runner/pipeline_v2.py``) import from here so the behavior is identical
everywhere.
"""

import json
import re
import subprocess

# Default budgets — match the values that produced the Stage 1/2 results.
FIX_TOOLS = "Read,Grep,Glob,Edit,Write"
LOCALIZE_TOOLS = "Read,Grep,Glob"
FIX_MAX_TURNS = "75"
LOCALIZE_MAX_TURNS = "50"
FIX_TIMEOUT = 1200
LOCALIZE_TIMEOUT = 900
BODY_CAP = 3000  # chars of issue body handed to the session

FIX_PROMPT = """You are fixing a real bug. Below is a GitHub issue for \
this repository (your current working directory is the repo root, checked \
out at the commit where the bug exists).

Investigate the root cause: explore with your tools and READ the relevant \
code until you understand the mechanism of the bug — not just where \
related words appear. Then EDIT the repository files to fix it. Make the \
smallest correct change; follow the surrounding code style. Do not add \
tests, comments about your reasoning, or unrelated cleanups.

When finished, output ONLY a JSON object on the last line:
{{"done": true, "files": ["files/you/modified.ext"], "summary": "<one sentence>"}}
If you conclude you cannot fix it confidently, make no edits and output:
{{"done": false, "files": [], "summary": "<why>"}}

ISSUE {key}: {title}

{body}
"""

LOCALIZE_PROMPT = """You are localizing a bug. Below is a GitHub issue for this \
repository (your current working directory is the repo root, checked out \
at the commit where the bug exists).

Explore the codebase with your tools and identify which file(s) must be \
MODIFIED to fix the issue. Look for the code that implements the faulty \
behavior — not just files that mention related words. Read the actual \
code before deciding.

When you are confident, output ONLY a JSON object on the last line, no \
other text after it:
{{"files": ["most/likely/file.ext", "second/choice.ext", "third/choice.ext"]}}

List 1-3 files, most likely first, paths relative to the repo root.

ISSUE {key}: {title}

{body}
"""


def _run(cmd, cwd=None, timeout=600, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          env=env)


def run_agentic_session(prompt, cwd, allowed_tools, max_turns, timeout,
                        env=None):
    """Spawn a constrained ``claude -p`` agentic session in ``cwd``.

    Returns the CompletedProcess. Raises subprocess.TimeoutExpired on budget
    overrun (callers decide what to salvage). ``env`` defaults to inheriting
    the parent environment, as the benchmarks do.
    """
    return subprocess.run(
        ["claude", "-p", prompt,
         "--allowedTools", allowed_tools,
         "--max-turns", str(max_turns)],
        cwd=cwd, capture_output=True, encoding="utf-8",
        errors="replace", timeout=timeout, env=env)


# ── fix generation ────────────────────────────────────────────────────────

def _parse_declared(out):
    """Last JSON object containing a "done" key, or {}."""
    for m in reversed(list(re.finditer(r"\{[^{}]*\"done\"[^{}]*\}", out))):
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
    return {}


def capture_diff(path, reset=True, env=None):
    """Stage everything (so new files show) and capture the diff vs HEAD.

    With ``reset=True`` (benchmark behavior) the tree is restored afterwards so
    the next session starts clean. Production passes ``reset=False`` to keep the
    working-tree changes for shipping.
    """
    # Purge AppleDouble ._* companions (exFAT) before staging, or they end up
    # as contentless binary entries that break git apply downstream.
    _run(["find", path, "-name", "._*", "-delete"], cwd=path, env=env)
    _run(["git", "add", "-A"], cwd=path, env=env)
    r = _run(["git", "diff", "--cached", "HEAD"], cwd=path, env=env)
    diff = r.stdout if r.returncode == 0 else ""
    if reset:
        _run(["git", "reset", "--hard", "-q"], cwd=path, env=env)
        _run(["git", "clean", "-fdq"], cwd=path, env=env)
    return diff


def agentic_fix(path, issue_key, title, body, *, tools=FIX_TOOLS,
                max_turns=FIX_MAX_TURNS, timeout=FIX_TIMEOUT, body_cap=BODY_CAP,
                reset=True, env=None):
    """Run the agentic fix session; return ``(diff_text, declared, error)``.

    On timeout, whatever diff exists is still captured (partial work), matching
    the Stage 2 benchmark semantics.
    """
    prompt = FIX_PROMPT.format(key=issue_key, title=title,
                               body=(body or "")[:body_cap])
    try:
        r = run_agentic_session(prompt, path, tools, max_turns, timeout, env)
    except subprocess.TimeoutExpired:
        return capture_diff(path, reset=reset, env=env), {}, "timeout"
    declared = _parse_declared((r.stdout or "").strip())
    err = ""
    if r.returncode != 0:
        detail = (r.stderr or "").strip() or (r.stdout or "")[-160:]
        err = f"exit_{r.returncode}: {detail[:160]}"
    return capture_diff(path, reset=reset, env=env), declared, err


# ── localization ──────────────────────────────────────────────────────────

def _parse_files(out):
    """Last JSON object containing "files" -> up to 3 normalized paths."""
    for m in reversed(list(re.finditer(r"\{[^{}]*\"files\"[^{}]*\}", out))):
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        files = []
        for f in data.get("files", []):
            if not isinstance(f, str) or not f.strip():
                continue
            f = f.strip()
            while f.startswith("./"):
                f = f[2:]
            files.append(f.lstrip("/"))
        if files:
            return files[:3], ""
    return [], "no_json: " + out[-160:].replace("\n", " ")


def agentic_localize(path, issue_key, title, body, *, tools=LOCALIZE_TOOLS,
                     max_turns=LOCALIZE_MAX_TURNS, timeout=LOCALIZE_TIMEOUT,
                     body_cap=BODY_CAP, env=None):
    """Run the agentic localization session; return ``(files, error)``."""
    prompt = LOCALIZE_PROMPT.format(key=issue_key, title=title,
                                    body=(body or "")[:body_cap])
    try:
        r = run_agentic_session(prompt, path, tools, max_turns, timeout, env)
    except subprocess.TimeoutExpired:
        return [], "timeout"
    if r.returncode != 0:
        detail = (r.stderr or "").strip() or (r.stdout or "").strip()[-160:]
        return [], f"exit_{r.returncode}: {detail[:160]}"
    return _parse_files((r.stdout or "").strip())
