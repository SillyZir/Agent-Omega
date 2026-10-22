"""LLM judge — two roles, kept distinct on purpose.

1. ``compare_patches`` (the **calibrated** Stage-2 judge): given the agent's
   patch AND the maintainer's merged fix, decide whether they address the same
   root cause. Stage 3 proved its ``equivalent`` verdict has a perfect
   execution record (3/3 validated), so it is a trustworthy quality proxy —
   *where ground truth exists* (the offline benchmark / eval harness).

2. ``review_patch`` (production reviewer): a *live* issue has no merged human
   fix to compare against, so production cannot use the calibrated comparison
   judge. Instead a single-patch reviewer assesses whether the patch plausibly
   fixes the issue. Its verdict is a gate toward **human review**, never an
   auto-ship — the calibration above does NOT transfer to this prompt.

Keeping both here makes the distinction explicit and prevents silently reusing
the comparison judge's calibration where it doesn't apply.
"""

import json
import os
import re
import subprocess
import time

JUDGE_TIMEOUT = 180
DIFF_CAP = 6000   # chars of each diff shown to the judge
BODY_CAP = 1200   # chars of issue body

# ── role 1: calibrated comparison judge (ground truth required) ────────────

_COMPARE_PROMPT = """Compare two patches for the same bug. Patch A was \
written by an automated agent; Patch B is the fix the human maintainer \
actually merged. Judge whether Patch A addresses the same root cause.

Respond with ONLY a JSON object, no other text:
{{"verdict": "equivalent" | "partial" | "different_approach" | "wrong", "reason": "<one sentence>"}}

- equivalent: same root cause, functionally the same fix
- partial: touches the right mechanism but incomplete or subtly off
- different_approach: plausibly correct fix via a different mechanism
- wrong: does not address the actual root cause

ISSUE: {title}

{body}

=== PATCH A (agent) ===
{agent_diff}

=== PATCH B (human, merged) ===
{human_diff}
"""

# Stage-3 calibration: only `equivalent` had a perfect execution record.
# Offline action mapping (used where ground truth exists).
COMPARE_ACTION = {
    "equivalent": "ship",          # 3/3 execution-validated
    "partial": "review",           # touches the mechanism; needs a human
    "different_approach": "reject",  # 0/1 validated in Stage 3
    "wrong": "reject",
}


def _ask(prompt, timeout=JUDGE_TIMEOUT, max_turns="4"):
    try:
        r = subprocess.run(["claude", "-p", prompt, "--max-turns", max_turns],
                           capture_output=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return (r.stdout or "").strip()


# Amendment 2026-08-26 (knowledge/amendment_design_judge_parser_2026-08-25.md):
# the fast regex below cannot span a verdict JSON whose `reason` contains
# braces (caddy#7721, reproduced 2x); _balanced_fallback covers that case.
# Deployed behind OMEGA_JUDGE_PARSER_V2 on 2026-09-03; the gate was removed
# per design §8 on 2026-09-19 after the clean week (§8b record) — the
# fallback is now unconditional. Rollback: single-commit revert.
_UNPARSED_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs", "judge_unparsed")


def _balanced_fallback(out):
    """Brace-balance-aware extraction: rightmost-first raw_decode from each
    '{', accepting the first dict that carries a "verdict" key. Strictly a
    fallback — runs only when the fast regex path found nothing."""
    dec = json.JSONDecoder()
    for i in range(len(out) - 1, -1, -1):
        if out[i] != "{":
            continue
        try:
            obj, _ = dec.raw_decode(out[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            return obj
    return None


def _retain_unparsed(out):
    """Persist the full judge stdout so the next instrument failure is
    diagnosable without inference (design §4b). Observability only: no
    consumer reads the returned path, and no failure here may break the
    judge — fail silent, return None."""
    try:
        os.makedirs(_UNPARSED_DIR, exist_ok=True)
        p = os.path.join(_UNPARSED_DIR,
                         f"{time.strftime('%Y%m%dT%H%M%S')}_{os.getpid()}.txt")
        with open(p, "a", encoding="utf-8") as f:
            f.write(out + "\n---\n")
        return p
    except Exception:
        # fail-silent by design (§4b): observability must never break the
        # judge, whatever the failure mode.
        return None


def _parse_verdict(out):
    if out is None:
        return {"verdict": "judge_timeout", "reason": ""}
    for m in reversed(list(re.finditer(r"\{[^{}]*\"verdict\"[^{}]*\}", out))):
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
    obj = _balanced_fallback(out)
    if obj is not None:
        return obj
    v = {"verdict": "judge_unparsed", "reason": out[-120:]}
    p = _retain_unparsed(out)
    if p:
        v["raw_capture"] = p
    return v


_INFRA_VERDICTS = ("judge_timeout", "judge_unparsed")


def _judged(prompt, timeout):
    """Ask + parse, with ONE retry when the failure is judge infrastructure
    (CLI timeout / turn-cap before the JSON line), not the patch. Audit:
    terraform#38660 — a tests-passing patch was rejected because the judge hit
    max-turns and the unparsed verdict fell through to 'reject'."""
    v = _parse_verdict(_ask(prompt, timeout))
    if v.get("verdict") in _INFRA_VERDICTS:
        retry = _parse_verdict(_ask(prompt, timeout))
        if retry.get("verdict") not in _INFRA_VERDICTS:
            return retry
        v["retried"] = True
    return v


def compare_patches(title, body, agent_diff, human_diff,
                    diff_cap=DIFF_CAP, body_cap=BODY_CAP, timeout=JUDGE_TIMEOUT):
    """Calibrated comparison judge. Returns {"verdict", "reason"}.

    verdict ∈ {equivalent, partial, different_approach, wrong} (+ judge_timeout
    / judge_unparsed on failure)."""
    prompt = _COMPARE_PROMPT.format(
        title=title, body=(body or "")[:body_cap],
        agent_diff=(agent_diff or "")[:diff_cap],
        human_diff=(human_diff or "")[:diff_cap])
    return _judged(prompt, timeout)


# ── role 2: production single-patch reviewer (no ground truth) ─────────────

_REVIEW_PROMPT = """You are reviewing an automated agent's patch for a real \
GitHub issue. There is no reference solution — judge the patch on its own \
merits: does it correctly address the root cause described in the issue, \
with the smallest sensible change and no obvious breakage?

{test_block}Respond with ONLY a JSON object, no other text:
{{"verdict": "correct" | "plausible" | "incorrect" | "insufficient", "reason": "<one sentence>"}}

- correct: clearly addresses the root cause; safe to propose to maintainers
- plausible: likely correct but warrants a careful human look
- incorrect: addresses the wrong thing or introduces a bug
- insufficient: no real change, or too little to fix the issue

ISSUE: {title}

{body}

=== AGENT PATCH ===
{agent_diff}
"""

# Production never auto-ships on the judge alone — every non-rejected patch is
# queued for human review. This maps a verdict to whether it's a ship candidate.
REVIEW_ACTION = {
    "correct": "review",      # strongest signal -> human-review queue
    "plausible": "review",
    "incorrect": "reject",
    "insufficient": "reject",
    # Judge INFRASTRUCTURE failures (after a retry) are inconclusive, not a
    # verdict on the patch — route to the human-review queue rather than
    # silently discarding a possibly-good patch. Production never auto-ships,
    # so this stays human-gated; failing tests still force reject upstream.
    "judge_timeout": "review",
    "judge_unparsed": "review",
}


def review_patch(title, body, agent_diff, test_summary="",
                 diff_cap=DIFF_CAP, body_cap=BODY_CAP, timeout=JUDGE_TIMEOUT):
    """Single-patch production reviewer. Returns {"verdict", "reason"}.

    verdict ∈ {correct, plausible, incorrect, insufficient}. Optionally
    conditioned on a one-line summary of the regression-harness result.
    """
    test_block = ""
    if test_summary:
        test_block = ("Regression-test result for this patch: "
                      f"{test_summary}\n\n")
    prompt = _REVIEW_PROMPT.format(
        title=title, body=(body or "")[:body_cap],
        agent_diff=(agent_diff or "")[:diff_cap], test_block=test_block)
    return _judged(prompt, timeout)


def gate(verdict, *, comparison=False):
    """Map a verdict to an action: 'ship' | 'review' | 'reject'.

    comparison=True uses the calibrated COMPARE_ACTION (ground-truth context);
    otherwise the production REVIEW_ACTION. Unknown/error verdicts -> 'reject'.
    """
    table = COMPARE_ACTION if comparison else REVIEW_ACTION
    return table.get(verdict, "reject")


# ── role 3: independent test-validity evaluator (v2.4 test generation) ──────
# Blind to the fix. Decides whether a generated regression test (which failed
# on the buggy code) reproduces the REPORTED behavior and asserts the real
# behavioral contract — not a superficial implementation detail. The #7905
# lesson: a test that only checks WriteHeader propagation, not a real flush,
# is superficial.
_TEST_VALIDITY_PROMPT = """You are evaluating an automatically-generated \
regression test for a reported bug. You are BLIND to any fix. Given the issue \
and the generated test — which FAILED on the current (buggy) code, with the \
failure output shown — judge whether the test:
1. reproduces the REPORTED behavior (the failure corresponds to the bug the \
issue describes), AND
2. asserts the real behavioral CONTRACT the issue is about — NOT a superficial \
implementation detail or an incidental property.

Respond with ONLY a JSON object, no other text:
{{"verdict": "valid" | "superficial" | "incorrect" | "insufficient", "reason": "<one sentence>"}}

- valid: the failure IS the reported bug AND the test checks the real, \
observable behavioral contract.
- superficial: the test keys on an implementation detail rather than the \
observable reported behavior (e.g. asserts that an internal method was called \
rather than the actual effect the issue describes).
- incorrect: the failure is unrelated to the reported bug, or the test asserts \
the wrong thing.
- insufficient: the test does not meaningfully exercise the reported behavior.

ISSUE: {title}

{body}

=== GENERATED TEST ===
{test}

=== ITS FAILURE OUTPUT ON THE BUGGY CODE ===
{failure}
"""

TEST_VALIDITY_ACTIONS = {"valid", "superficial", "incorrect", "insufficient"}


def judge_test_validity(title, body, test_text, failure_output, *,
                        diff_cap=DIFF_CAP, body_cap=BODY_CAP,
                        fail_cap=1500, timeout=JUDGE_TIMEOUT):
    """Independent, fix-blind evaluator for a generated regression test.

    Returns {"verdict", "reason"} with verdict ∈ {valid, superficial,
    incorrect, insufficient} (+ judge_timeout/judge_unparsed on infra failure,
    which the caller must treat as fail-closed → not 'valid')."""
    prompt = _TEST_VALIDITY_PROMPT.format(
        title=title, body=(body or "")[:body_cap],
        test=(test_text or "")[:diff_cap],
        failure=(failure_output or "")[:fail_cap])
    return _judged(prompt, timeout)
