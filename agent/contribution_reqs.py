"""Repository contribution-awareness — detect and satisfy repo-specific PR
requirements so shipped PRs pass repo CI, not just compile.

Surfaced in production: hashicorp/terraform's "Enforce Changelog" CI failed on
the first v2 PR because the agentic fix added no `.changes/*.yaml` fragment.

Everything is detected from the checkout (never hardcoded per repo):

  * changelog — full support for **changie** (`.changie.yaml`): generate a
    fragment in `<changesDir>/<unreleasedDir>/`. Fragment-dir mechanisms
    (towncrier `changelog.d/`) are detected and surfaced, not auto-generated.
    A hand-edited markdown `CHANGELOG.md` is detected as MANDATORY when the
    repo also *states* the requirement (see `_CHANGELOG_REQ`), and is rendered
    only for repos with an explicit profile in `_MARKDOWN_PROFILES`; every
    other markdown repo becomes a compliance blocker rather than a silent pass
    (design_markdown_changelog_2026-09-03.md §1A).
  * DCO — if CONTRIBUTING requires a Developer Certificate of Origin, commits
    are signed off (`git commit -s`). That is the human contributor's
    certification, not AI attribution.
  * CLA — detected and surfaced; a human must sign via the CLA bot (cannot be
    automated).
  * PR template — used as the PR body base when present.
"""

import datetime
import json
import os
import re

_CONTRIB_PATHS = ("CONTRIBUTING.md", ".github/CONTRIBUTING.md",
                  "docs/CONTRIBUTING.md")
_PRTEMPLATE_PATHS = (".github/PULL_REQUEST_TEMPLATE.md",
                     ".github/pull_request_template.md",
                     "PULL_REQUEST_TEMPLATE.md",
                     "docs/PULL_REQUEST_TEMPLATE.md")
_FRAGMENT_DIRS = ("changelog.d", ".changelog", "newsfragments", "changes.d")

# Files consulted for evidence that a markdown CHANGELOG.md is REQUIRED.
# Deliberately a changelog-only list: adding AGENTS.md to _CONTRIB_PATHS would
# silently change DCO/CLA detection for every repo that ships one.
_CHANGELOG_EVIDENCE_PATHS = _CONTRIB_PATHS + ("AGENTS.md", ".github/AGENTS.md")

_CHANGELOG_REQ = re.compile(
    r"^\s{0,3}#{1,4}\s*changelog\b"                       # a "## Changelog" section
    r"|changelog\s+entry"                                 # "Changelog entry added..."
    r"|add\s+(?:an?\s+|the\s+)?entr(?:y|ies)\s+to\s+\S{0,4}changelog"
    r"|update\s+(?:the\s+)?\S{0,4}changelog",
    re.IGNORECASE | re.MULTILINE)

# A line that MENTIONS a changelog is not a line that REQUIRES one. Repos
# routinely write "no changelog entry is needed for docs changes" or
# "maintainers will update the changelog" — matched verbatim by the patterns
# above (audit BUG4-logic). A matched line carrying any of these is discarded.
# This deliberately over-discards ("do not forget to update the changelog"
# reads as a negation here); the error costs are asymmetric — see
# `_changelog_requirement_evidence`.
_CHANGELOG_NEG = re.compile(
    r"\bno\b|\bnot\b|\bn/a\b|\bnever\b|\bwithout\b|\bunnecessary\b"
    r"|\bdon'?t\b|\bdoesn'?t\b|\bskip\b|\bexempt\b|\boptional\b"
    r"|\bmaintainers?\b|\breleng\b",
    re.IGNORECASE)


class ChangelogError(Exception):
    """A MANDATORY changelog entry could not be rendered. Raised (never
    returned) so the ship path cannot silently proceed without one.

    `transient` distinguishes "not renderable RIGHT NOW, for this patch and
    this checkout" from "not renderable at all until someone writes code".
    The distinction decides whether the issue is retried or retired, so it is
    carried on the exception rather than re-derived by string-matching the
    reason downstream (round-2 audit F1)."""

    def __init__(self, message, *, transient=False):
        super().__init__(message)
        self.transient = transient


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _parse_changie(text):
    """Minimal extraction of the fields we need from `.changie.yaml`."""
    def scalar(key):
        m = re.search(rf"^{key}:\s*(.+)$", text, re.M)
        return m.group(1).strip().strip("'\"") if m else None
    # kinds: block -> "- label: X" lines that appear after a `kinds:` line
    kinds = []
    in_kinds = False
    for line in text.splitlines():
        if re.match(r"^kinds:\s*$", line):
            in_kinds = True
            continue
        if in_kinds:
            if re.match(r"^\S", line):        # dedented -> block ended
                break
            m = re.match(r"\s*-\s*label:\s*(.+)$", line)  # only list items
            if m:
                kinds.append(m.group(1).strip().strip("'\""))
    return {"changes_dir": scalar("changesDir") or ".changes",
            "unreleased_dir": scalar("unreleasedDir"),
            "kinds": kinds}


def _changelog_requirement_evidence(path):
    """Return a short evidence string if the repo STATES that PRs need a
    changelog entry, else None. Existence of CHANGELOG.md alone is not a rule:
    absence of a stated requirement keeps the historical note-only behaviour.

    DOCUMENTED requirements only. The design (§4) also proposed treating a
    changelog-referencing CI workflow as evidence; that was DROPPED after
    measurement against the live fleet — release-automation workflows mention
    changelogs constantly, and both `esengine/DeepSeek-Reasonix`
    (release-notes.yml) and `grafana/grafana` (release-comms.yml) false-
    positived even after narrowing to `pull_request`-triggered workflows.

    The error costs are asymmetric: MISSING a requirement means shipping
    without an entry (a bot nag — soft, visible, recoverable), while FALSELY
    inferring one sets `blocked_compliance` and stops the repo shipping
    entirely. So this infers only from prose a maintainer actually wrote.
    Known accepted gap: a repo that enforces a changelog in CI without
    documenting it anywhere is not detected here. Same asymmetry drives the
    `_CHANGELOG_NEG` filter: a mention wrapped in a negation is discarded even
    at the cost of discarding some genuine requirements."""
    for cf in _CHANGELOG_EVIDENCE_PATHS:
        p = os.path.join(path, cf)
        if not os.path.isfile(p):
            continue
        text = _read(p)
        for m in _CHANGELOG_REQ.finditer(text):
            ls = text.rfind("\n", 0, m.start()) + 1
            le = text.find("\n", m.end())
            line = text[ls:le if le != -1 else len(text)]
            if not _CHANGELOG_NEG.search(line):
                return cf
    return None


def detect(path):
    """Inspect a checkout; return a requirements dict:
    {changelog: {...}|None, dco: bool, cla: bool, pr_template: str|None,
     notes: [str]}."""
    reqs = {"changelog": None, "dco": False, "cla": False,
            "pr_template": None, "notes": []}

    changie = os.path.join(path, ".changie.yaml")
    if os.path.isfile(changie):
        cfg = _parse_changie(_read(changie))
        if cfg.get("unreleased_dir"):
            reqs["changelog"] = {"kind": "changie", **cfg}
        else:
            reqs["notes"].append(".changie.yaml present but no unreleasedDir; "
                                 "changelog fragment not auto-generated")
    else:
        frag = next((d for d in _FRAGMENT_DIRS
                     if os.path.isdir(os.path.join(path, d))), None)
        if frag:
            reqs["changelog"] = {"kind": "fragments", "dir": frag}
            reqs["notes"].append(f"changelog fragments under {frag}/ "
                                 "(auto-generation not supported — manual entry)")
        elif os.path.isfile(os.path.join(path, "CHANGELOG.md")):
            ev = _changelog_requirement_evidence(path)
            if ev:
                # Requirement is STATED, not merely implied by the file's
                # existence — mandatory. Whether we can satisfy it is a
                # separate question (markdown_supported / _MARKDOWN_PROFILES);
                # if we cannot, the compliance gate blocks rather than passing
                # silently.
                reqs["changelog"] = {"kind": "markdown", "file": "CHANGELOG.md",
                                     "evidence": ev}
            else:
                reqs["notes"].append("CHANGELOG.md present; a PR may need an "
                                     "entry (not auto-generated)")

    for cf in _CONTRIB_PATHS:
        p = os.path.join(path, cf)
        if os.path.isfile(p):
            t = _read(p).lower()
            if ("signed-off-by" in t or "developer certificate of origin" in t
                    or re.search(r"\bdco\b", t)):
                reqs["dco"] = True
            if ("contributor license agreement" in t
                    or re.search(r"\bcla\b", t)):
                reqs["cla"] = True

    for tp in _PRTEMPLATE_PATHS:
        p = os.path.join(path, tp)
        if os.path.isfile(p):
            reqs["pr_template"] = _read(p)
            break

    if reqs["cla"]:
        reqs["notes"].append("CLA required — a human must sign via the CLA bot "
                             "on first contribution (cannot be automated)")
    if reqs["dco"]:
        reqs["notes"].append("DCO required — commits will be signed off")
    return reqs


def _pick_kind(kinds):
    """Prefer a bug-fix kind; else the first; else a safe default."""
    for k in kinds or ():
        if "bug" in k.lower():
            return k
    return (kinds or ["BUG FIXES"])[0]


def changelog_fragment(reqs, issue, summary, now=None):
    """For a changie repo, return (relpath, content) for a new fragment, or None.

    Filename deliberately avoids spaces (git quotes space-paths, which breaks
    diff-parsing and `git apply`); changie reads the `kind:` from the content,
    not the filename, and the CI gate only checks `.changes/*/*.yaml` exists.
    """
    cl = reqs.get("changelog") or {}
    if cl.get("kind") != "changie" or not cl.get("unreleased_dir"):
        return None
    now = now or datetime.datetime.now().astimezone()
    kind = _pick_kind(cl.get("kinds"))
    ts = now.strftime("%Y%m%d-%H%M%S")
    # Sanitize the kind into the filename: strip ALL non-alphanumerics (spaces,
    # slashes, …) so the fragment can't land in a subdir changie won't scan and
    # git never has to quote a space-path.
    slug = re.sub(r"[^A-Za-z0-9]+", "-", kind).strip("-") or "CHANGE"
    fname = f"{slug}-{ts}.yaml"
    relpath = os.path.join(cl["changes_dir"], cl["unreleased_dir"], fname)
    body = " ".join((summary or "Fix issue").split()).rstrip(".")
    num = issue.get("number") or ""
    # `body` is free text from the agent (often contains ": ", "@", leading "-",
    # etc.) — emit it as a JSON string, which is a valid YAML double-quoted
    # scalar, so the fragment always parses. `kind` comes from the repo's own
    # config (a controlled label), so it stays a plain scalar to match style.
    content = (f"kind: {kind}\n"
               f"body: {json.dumps(body)}\n"
               f"time: {now.isoformat()}\n")
    if num:
        content += f"custom:\n    Issue: \"{num}\"\n"
    return relpath, content


# ── markdown CHANGELOG.md support ───────────────────────────────────────────

# Directory segments that mark a path as test-only for subsystem derivation.
# `tests` and `testutil` are real top-level cosmos-sdk directories and were
# missing from the first cut (audit BUG3-logic): a fix touching only
# `tests/integration/...` derived no tag and hard-blocked the ship.
_TEST_DIR_SEGMENTS = ("test", "tests", "testdata", "testutil", "testutils",
                      "__tests__", "spec")


def _is_test_path(f):
    p = (f or "").replace("\\", "/").lower()
    parts = p.split("/")
    if any(seg in _TEST_DIR_SEGMENTS for seg in parts[:-1]):
        return True
    base = parts[-1]
    return ("_test." in base or base.startswith("test_")
            or ".test." in base or ".spec." in base)


# The changelog ARTIFACT, matched in two independent ways: a changelog
# DIRECTORY anywhere in the path, or a changelog-ish BASENAME whose extension
# is a document extension. Round 1 narrowed a too-broad "contains the word
# changelog" pattern (which wrongly excluded production source like
# `x/auth/changelog.go`) and in doing so stopped matching `docs/changelog/*`,
# `changelog/unreleased/*.yaml` and an extensionless `CHANGELOG` — a fix that
# introduced three regressions (round-2 audit F8). Both forms are needed.
_CHANGELOG_DIR_HINT = re.compile(
    r"(^|/)(\.changes?|\.changeset|changelog(\.d)?|changes\.d|newsfragments)/",
    re.IGNORECASE)
_CHANGELOG_NAME_HINT = re.compile(
    r"^(changelog|changes|history|news|release[_-]?notes)[^./]*"
    r"(\.(md|markdown|rst|txt|ya?ml|json))?$"
    r"|^\.changie\.ya?ml$",
    re.IGNORECASE)


def _is_changelog_artifact(f):
    """True for changelog files/fragments, false for source merely NAMED like
    one. `changelog.go` fails the extension allowlist; `CHANGELOG` (no
    extension) and `docs/changelog/entry.md` both pass."""
    p = (f or "").replace("\\", "/")
    return bool(_CHANGELOG_DIR_HINT.search(p)
                or _CHANGELOG_NAME_HINT.match(p.rsplit("/", 1)[-1]))


def _core_files(changed_files):
    """Files that determine the subsystem: neither tests nor changelog.

    Falls back to the test files when a patch is TEST-ONLY. Excluding tests is
    right for choosing a subsystem when production files are present, but a
    test-only patch is a real, precedented changelog entry — cosmos's own file
    carries `* (baseapp) [#22368](...) Add \\`-race\\`-mode regression test ...`
    — and returning [] made it underivable, which after the round-1 probe
    became a PERMANENT block (round-2 audit F7)."""
    files = [f for f in (changed_files or []) if f and not _is_changelog_artifact(f)]
    core = [f for f in files if not _is_test_path(f)]
    return core or files


def cosmos_tag_vocabulary(changelog_text):
    """Every `(tag)` historically used in the repo's own CHANGELOG.md.

    Using the live file as the oracle means the rule tracks the repo instead of
    a hard-coded list going stale. `<tag>` is the template placeholder in the
    file header and is excluded."""
    return {t for t in re.findall(r"^\* \(([^)]+)\)", changelog_text, re.M)
            if t != "<tag>"}


def cosmos_tag(changed_files, vocabulary):
    """Derive the `(tag)` from changed paths, validated against the repo's own
    vocabulary. Returns None (fail closed) if it cannot be derived or is not a
    tag the repo actually uses — we never invent one."""
    core = _core_files(changed_files)
    if not core:
        return None
    dirs = [os.path.dirname(f) for f in core]
    if not all(dirs):
        return None                      # a repo-root file: no subsystem
    try:
        prefix = os.path.commonpath(dirs)
    except ValueError:                   # mixed absolute/relative
        return None
    parts = [p for p in prefix.split(os.sep) if p]
    if not parts:
        return None
    # Walk the path from most specific to least, taking the first candidate the
    # repo has actually used: `x/auth/ante` before `x/auth` before `x`.
    for n in range(len(parts), 0, -1):
        cand = "/".join(parts[:n])
        if cand in vocabulary:
            return cand
    return None


# Trailing spaces/tabs, then a LOOKAHEAD at the line terminator so the match
# consumes none of it. Two round-1 bugs came from getting this wrong:
#   * `\s*$` under re.M — `\s` matches `\n`, so the match ate the heading's own
#     terminator and every downstream offset shifted (BUG2-logic).
#   * `[^\S\n]*$` — `\s` still includes `\r`, so on a CRLF file the match ate
#     the `\r` and the empty-stanza branch then re-emitted `\r\n`, rewriting a
#     maintainer's heading line as `### Bug Fixes\r\r\n` (round-2 audit F1).
# A zero-width lookahead cannot eat anything, which is the property we want.
_EOL = r"[^\S\r\n]*(?=\r?\n|\Z)"

# ``` or ~~~ opening/closing a fenced block, at the start of a line.
_FENCE = re.compile(r"^[^\S\r\n]{0,3}(```|~~~)", re.M)


def _fence_spans(text):
    """Char ranges covered by fenced code blocks. Headings inside a fence are
    not headings; treating them as such truncated the section mid-fence and
    wrote our bullet INTO a maintainer's code sample (round-2 audit F2)."""
    marks = [m.start() for m in _FENCE.finditer(text)]
    return [(marks[i], marks[i + 1]) for i in range(0, len(marks) - 1, 2)] + \
           ([(marks[-1], len(text))] if len(marks) % 2 else [])


def _outside(spans):
    return lambda pos: not any(a <= pos < b for a, b in spans)


def _split_unreleased(text):
    """Return (start, end) char offsets of the `## UNRELEASED` section body, or
    None if it is absent or ambiguous. Fail closed on anything unexpected."""
    ok = _outside(_fence_spans(text))
    heads = [m for m in re.finditer(rf"^##[^\S\r\n]+UNRELEASED{_EOL}",
                                    text, re.M | re.I) if ok(m.start())]
    if len(heads) != 1:
        return None
    start = heads[0].end()
    nxt = next((m for m in re.finditer(r"^##[^\S\r\n]+(?!#)", text[start:], re.M)
                if ok(start + m.start())), None)
    return (start, start + nxt.start() if nxt else len(text))


def _logical_entries(lines):
    """Group a stanza body into logical entries: each TOP-LEVEL `* ` line plus
    the indented continuation/sub-bullet lines that belong to it, whitespace-
    normalised. Used only for the idempotency test — placement stays line-based
    and never depends on this."""
    out, cur = [], None
    for ln in lines:
        if re.match(r"^[*+-][^\S\r\n]", ln):          # a new top-level entry
            if cur is not None:
                out.append(" ".join(cur.split()))
            cur = ln
        elif cur is not None and ln.strip():
            cur += " " + ln
        elif not ln.strip() and cur is not None:
            out.append(" ".join(cur.split()))
            cur = None
    if cur is not None:
        out.append(" ".join(cur.split()))
    return out


def _has_entry(lines, bullet):
    """Whether this stanza already carries `bullet` as an entry of its own.

    Round 1 used `ln.strip() == bullet.strip()`, which was wrong in BOTH
    directions (round-2 audit F4/F5):
      * FALSE POSITIVE — `.strip()` discards indentation, so a bullet appearing
        only as someone else's SUB-bullet matched, the write was skipped, and
        `_cosmos_entry` still returned success. The required entry was absent
        while the compliance gate believed it was written. That is the worst
        outcome available here: a silent no-op reported as done.
      * FALSE NEGATIVE — an entry a maintainer hard-wrapped across two lines
        never matched, so a byte-identical duplicate was appended.
    Comparing whitespace-normalised LOGICAL entries fixes both: a sub-bullet is
    only ever part of its parent's entry, and wrapping normalises away."""
    want = " ".join(bullet.split())
    return want in _logical_entries(lines)


def insert_markdown_entry(text, stanza, bullet):
    """Append `bullet` to the `### <stanza>` stanza inside `## UNRELEASED`.
    Returns the new text, or None if the structure is not exactly as expected
    (missing/duplicated heading) — we never synthesise a stanza, because
    stanza order is meaningful in these files.

    Idempotent: if the stanza already carries this entry — including one a
    maintainer has since hard-wrapped across lines — returns the text
    unchanged.

    Placement is LINE-based, after the last non-blank line of the stanza body.
    The original regex matched an entry as "a `* ` line plus following lines
    that start with neither `*`, `#` nor whitespace", which ends an entry at
    its first *indented* continuation — 32 of cosmos-sdk's 553 live bullets
    wrap that way. Inserting there put our bullet between a maintainer's entry
    and its own sub-bullets, silently reparenting their sub-list under ours: a
    corrupting write where the contract is fail-closed (audit BUG1)."""
    span = _split_unreleased(text)
    if not span:
        return None
    s, e = span
    section = text[s:e]
    if _FENCE.search(section):
        return None       # fenced code inside UNRELEASED — refuse to guess
    sh = list(re.finditer(rf"^###[^\S\r\n]+{re.escape(stanza)}{_EOL}",
                          section, re.M))
    if len(sh) != 1:
        return None
    body_start = sh[0].end()
    # Any DEEPER heading also ends the region we append to: `#### Notes` is a
    # sub-section of this stanza, and appending after the last non-blank line
    # of the whole stanza would file our entry under that sub-heading, glued to
    # its prose (round-2 audit F3). go-task's CHANGELOG.md uses h4 this way.
    nxt = re.search(r"^#{2,6}[^\S\r\n]", section[body_start:], re.M)
    body_end = body_start + nxt.start() if nxt else len(section)
    body = section[body_start:body_end]

    # Line ending from THIS region, not the whole file: one stray CRLF in a
    # 2019 release section must not decide how we terminate today's bullet
    # (round-2 audit F6).
    m_nl = re.search(r"\r?\n", section[body_start:]) or re.search(r"\r?\n", text)
    nl = m_nl.group(0) if m_nl else "\n"
    lines = body.splitlines(keepends=True)
    if _has_entry(lines, bullet):
        return text                                   # already present
    last = max((i for i, ln in enumerate(lines) if ln.strip()), default=-1)
    if last < 0:
        # Empty stanza: heading, blank line, bullet, blank line before whatever
        # heading follows.
        new_body = nl + nl + bullet + nl
        tail = section[body_end:] + text[e:]
        if tail and not tail.startswith(("\n", "\r")):
            new_body += nl
    else:
        head = lines[:last + 1]
        if not head[-1].endswith(("\n", "\r")):       # no trailing newline
            head[-1] += nl
        new_body = "".join(head) + bullet + nl + "".join(lines[last + 1:])
    return (text[:s] + section[:body_start] + new_body
            + section[body_end:] + text[e:])


def _cosmos_entry(path, repo, issue, summary, changed_files, write=True):
    """Render and insert a cosmos-sdk changelog bullet. Raises ChangelogError
    with a specific reason on any failure — never returns a half-result.

    `write=False` runs every check and the full render but touches nothing;
    that is what `changelog_renderable` uses so the compliance gate and the
    ship path cannot disagree (audit BUG5-int)."""
    full = os.path.join(path, "CHANGELOG.md")
    text = _read(full)
    if not text:
        raise ChangelogError("CHANGELOG.md unreadable or empty", transient=True)
    tag = cosmos_tag(changed_files, cosmos_tag_vocabulary(text))
    if not tag:
        # Transient: depends on THIS patch's paths and on the tag vocabulary
        # parsed from the live file, which shrinks when a release truncates it.
        raise ChangelogError(
            "cannot derive a changelog tag from "
            f"{_core_files(changed_files)} that the repo already uses",
            transient=True)
    num = (issue or {}).get("number")
    if not num:
        raise ChangelogError("no issue number for the changelog reference",
                             transient=True)
    msg = " ".join((summary or "").split()).rstrip(".")
    if not msg:
        raise ChangelogError("empty summary — refusing to write a blank entry",
                             transient=True)
    # The PR number does not exist until after `gh pr create`, and nothing
    # pushes to the branch post-open, so the entry references the ISSUE. That
    # form is attested in this repo's own changelog but is a ~4% minority
    # (design §5.2) — a deliberate, recorded trade.
    bullet = (f"* ({tag}) [#{num}](https://github.com/{repo}/issues/{num}) "
              f"{msg}.")
    out = insert_markdown_entry(text, "Bug Fixes", bullet)
    if out is None:
        # Transient: the overwhelmingly common cause is the post-release window
        # where `## UNRELEASED` has just been renamed to a version heading and
        # not yet re-created. That is a statement about the calendar, not about
        # this issue, and must not retire it.
        raise ChangelogError(
            "CHANGELOG.md structure unexpected: need exactly one "
            "'## UNRELEASED' with one '### Bug Fixes' stanza, no fenced code",
            transient=True)
    if write and out != text:
        with open(full, "w", encoding="utf-8") as f:
            f.write(out)
    return "CHANGELOG.md"


# Markdown rendering is per-repo because changelog conventions are. A repo
# absent here is DETECTED but not satisfiable -> compliance blocker, never a
# silent pass. Adding a repo is additive and inherits nothing by accident.
_MARKDOWN_PROFILES = {"cosmos/cosmos-sdk": _cosmos_entry}


def markdown_supported(repo):
    return (repo or "") in _MARKDOWN_PROFILES


def changelog_renderable(path, reqs, issue, summary, *, repo=None,
                         changed_files=None):
    """Dry-run the markdown changelog render. Returns
    `(ok, reason, transient)`.

    `markdown_supported(repo)` answers "is there a profile for this repo",
    which is what the compliance gate used to ask on its own. That is not the
    same question as "will the render succeed on THIS patch": the tag may not
    be derivable from these paths, or the live CHANGELOG.md may be in its
    post-release state with no `## UNRELEASED` section at all — a window that
    recurs every release. Divergence there means the gate passes a bundle the
    ship path then rejects with `changelog_failed` (audit BUG5-int).

    `transient` matters because the two failure classes deserve opposite
    dispositions. A missing renderer is permanent and SHOULD block the repo
    until someone writes one. A release window is not about this issue at all,
    and routing it to `blocked_compliance` — which is terminal on the first
    record — would permanently retire a good candidate for a calendar
    coincidence (round-2 audit F1). Non-markdown mechanisms are not probed.

    Never raises: any unexpected exception from the profile is reported as a
    transient failure rather than escaping into the caller's error path."""
    cl = (reqs or {}).get("changelog") or {}
    if cl.get("kind") != "markdown":
        return True, None, False
    fn = _MARKDOWN_PROFILES.get(repo or "")
    if not fn:
        return False, (f"{repo or 'repo'} requires a markdown CHANGELOG.md "
                       "entry and no renderer profile exists for it"), False
    try:
        fn(path, repo, issue, summary, changed_files, write=False)
    except ChangelogError as exc:
        return False, str(exc), bool(getattr(exc, "transient", False))
    except Exception as exc:                      # noqa: BLE001 — see docstring
        return False, f"changelog probe error: {type(exc).__name__}: {exc}", True
    return True, None, False


def write_changelog(path, reqs, issue, summary, now=None, *,
                    repo=None, changed_files=None):
    """Write the repo's changelog artifact into the checkout; return its
    repo-relative path, or None if not applicable.

    Raises ChangelogError when a MANDATORY markdown entry cannot be rendered,
    so the caller fails closed instead of shipping without one."""
    cl = (reqs or {}).get("changelog") or {}
    if cl.get("kind") == "markdown":
        fn = _MARKDOWN_PROFILES.get(repo or "")
        if not fn:
            raise ChangelogError(
                f"{repo or 'repo'} requires a markdown CHANGELOG.md entry and "
                "no renderer profile exists for it")
        return fn(path, repo, issue, summary, changed_files)

    frag = changelog_fragment(reqs, issue, summary, now)
    if not frag:
        return None
    relpath, content = frag
    full = os.path.join(path, relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return relpath


def disclose_ai_title(title, marker):
    """Prepend a required AI-disclosure marker (e.g. terraform's ``🤖🤖🤖``) to a
    PR title if not already present. Honors the repo's self-disclosure policy."""
    if not marker:
        return title
    title = title or ""
    return title if marker in title else f"{marker} {title}".strip()


# Operator-directed format (ruling 2026-09-02): the operator's "engineering
# system I created" sentence, with the AI qualifier and tool name retained —
# disclosure only fires on repos that MANDATE disclosing AI use (mandate-only
# ruling 2026-09-01), and several of them require the tool named. Passive
# "was reviewed and tested" (not "I reviewed") because this constant also
# serves the autoship path, where no human pre-reviewed.
AI_DISCLOSURE_BODY = (
    "### Assistance Disclosure\n"
    "This patch was produced with the assistance of an AI engineering "
    "system I created (Claude, via an automated fix pipeline with test and "
    "review gates). It was reviewed and tested before submission, and is "
    "submitted from a human-owned account that takes responsibility for the "
    "change.")


def pr_body(issue, summary, changed_files, reqs, *, ai_disclose=False):
    """Compose a PR body. If the repo ships a PR template, prepend our summary
    and include the template for a human to complete. ``ai_disclose=True``
    appends the assistance-disclosure section for repos whose policy requires
    disclosure in the PR body (no title marker)."""
    num = issue.get("number")
    lines = []
    if num:
        lines.append(f"Fixes #{num}.")
        lines.append("")
    if summary:
        lines.append(summary.strip())
        lines.append("")
    code = [f for f in changed_files if not f.startswith((".changes/",))][:6]
    if code:
        lines.append("Files: " + ", ".join(f"`{f}`" for f in code))
    tmpl = ((reqs or {}).get("pr_template") or "").strip()
    if ai_disclose and tmpl and "assistance disclosure" in tmpl.lower():
        # The template has its own disclosure section (e.g. caddy) — fill it
        # in place instead of emitting a duplicate section above a leftover
        # "missing disclosure" placeholder (audit finding on PR #7898).
        note = AI_DISCLOSURE_BODY.split("\n", 1)[1]
        placeholder = "_This PR is missing an assistance disclosure._"
        if placeholder in tmpl:
            tmpl = tmpl.replace(placeholder, note)
        else:
            # Unknown placeholder wording — append the full section WITH its
            # heading so the disclosure is findable even if the template's
            # own placeholder text survives above it (audit v2.3.5).
            tmpl += "\n\n" + AI_DISCLOSURE_BODY
        ai_disclose = False
    if ai_disclose:
        lines += ["", AI_DISCLOSURE_BODY]
    if tmpl:
        lines += ["", "---", "", tmpl]
    return "\n".join(lines).strip()
