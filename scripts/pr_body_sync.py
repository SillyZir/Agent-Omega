#!/usr/bin/env python3
"""Sync a tracked PR's body after a rework (audit-v2 F1).

Transport + safety only — the updated body is drafted in-session and
operator-approved; this tool never generates or alters prose itself.
Design: knowledge/design_pr_body_sync_2026-09.md. Interactive use only;
no chain/cron path may invoke it.

  draft:  pr_body_sync.py OWNER/REPO NUM
  apply:  pr_body_sync.py OWNER/REPO NUM --apply NEW.md --note "..." --yes

Hardened per the 2026-09-02 adversarial audit: disclosure containment is
checked against the VISIBLE new body (HTML comments and fenced code
stripped); the block scanner is fence-aware and recognizes setext
headings; marker matching is whitespace-normalized; bodies are
CRLF-normalized; only OPEN PRs are editable; the ledger row is written
ahead of the outward edit; the body is re-fetched immediately before the
edit and any drift refuses.
"""
import argparse, difflib, hashlib, json, os, re, subprocess, sys, tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKED = os.path.join(ROOT, "ops_tracked_prs.json")
LEDGER = os.path.join(ROOT, "cloud", "pr_body_amendments.jsonl")
OUR_LOGIN = "sillyzir"  # compared casefolded
# Disclosure protection (see module docstring). Markers are matched with
# whitespace collapsed, case-insensitive, so hard-wrapped or re-wrapped
# paragraphs stay protected.
DISCLOSURE_MARKERS = (
    "assistance disclosure",
    "an ai engineering system",
    "engineering system operated by me",
    "engineering system i created",
)
_HR = re.compile(r"-{3,}|\*{3,}|_{3,}")
_UNDERLINE = re.compile(r"=+|-+")


def run_gh(args):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"gh {' '.join(args[:3])}... failed: {r.stderr.strip()[:300]}")
    return r.stdout


def _normalize(text):
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _squash(text):
    return " ".join(text.split()).lower()


def strip_hidden(text):
    """Remove HTML comments and fenced code — the parts of a rendered PR
    body a reader does not see as prose. Containment runs against this,
    so a byte-identical disclosure smuggled into a comment or fence does
    NOT count as preserved (audit P1)."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    out, in_fence = [], False
    for ln in text.splitlines():
        if ln.strip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(ln)
    return "\n".join(out)


def disclosure_paragraphs(body):
    body = _normalize(body)
    out = []
    for para in body.split("\n\n"):
        if any(m in _squash(para) for m in DISCLOSURE_MARKERS):
            out.append(para.strip())
    # heading-block protection: a "...disclosure..." heading (ATX or
    # setext) covers every line until the next heading or horizontal
    # rule; fences never open/close blocks (audit P2s)
    lines = body.splitlines()
    n = len(lines)
    blocks, block, in_fence = [], None, False

    def close():
        nonlocal block
        if block is not None:
            blk = "\n".join(block).strip()
            if blk:
                blocks.append(blk)
        block = None

    i = 0
    while i < n:
        ln = lines[i]
        s = ln.strip()
        if s.startswith(("```", "~~~")):
            in_fence = not in_fence
            if block is not None:
                block.append(ln)
            i += 1
            continue
        if in_fence:
            if block is not None:
                block.append(ln)
            i += 1
            continue
        nxt = lines[i + 1].strip() if i + 1 < n else ""
        is_atx = s.startswith("#")
        is_setext = bool(s) and not is_atx and bool(nxt) and bool(_UNDERLINE.fullmatch(nxt))
        is_hr = bool(_HR.fullmatch(s))
        if is_atx or is_setext or is_hr:
            close()
        opened = False
        if (is_atx or is_setext) and "disclosure" in s.lower():
            block, opened = [ln], True
            if is_setext:
                block.append(lines[i + 1])
                i += 2
                continue
        if block is not None and not opened:
            block.append(ln)
        i += 1
    close()
    return out + blocks


def guard(repo, num, tracked_file):
    tracked = json.load(open(tracked_file))
    if not any(t["repo"] == repo and t["number"] == num for t in tracked):
        raise SystemExit(f"REFUSED: {repo}#{num} is not in ops_tracked_prs.json — this tool edits our tracked PRs only.")
    meta = json.loads(run_gh(["pr", "view", str(num), "--repo", repo,
                              "--json", "author,body,url,state"]))
    if (meta["author"].get("login") or "").lower() != OUR_LOGIN:
        raise SystemExit(f"REFUSED: {repo}#{num} author is {meta['author'].get('login')!r}, not ours — never edit a PR that is not ours.")
    if meta.get("state") != "OPEN":
        raise SystemExit(f"REFUSED: {repo}#{num} is {meta.get('state')} — only OPEN PRs may be edited; a merged/closed body is part of the reviewed record (audit P2).")
    return meta


def _append_ledger(path, row):
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("number", type=int)
    ap.add_argument("--apply", metavar="NEWBODY_MD", help="file containing the operator-approved new body")
    ap.add_argument("--note", default="", help="operator approval context (required with --apply)")
    ap.add_argument("--yes", action="store_true", help="confirm after reviewing the printed diff")
    ap.add_argument("--tracked-file", default=TRACKED, help=argparse.SUPPRESS)
    ap.add_argument("--ledger", default=LEDGER, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    meta = guard(a.repo, a.number, a.tracked_file)
    old = _normalize(meta.get("body"))

    if not a.apply:
        print(old)
        print(f"\n-- current body of {a.repo}#{a.number} ({meta['state']}) above; "
              f"draft the update in-session, get operator approval, then re-run with --apply", file=sys.stderr)
        return 0

    if not a.note.strip():
        raise SystemExit("REFUSED: --apply requires --note with the operator approval context (per-item approval, EQ-1 discipline).")
    with open(a.apply) as f:
        new = _normalize(f.read())
    if not new.strip():
        raise SystemExit("REFUSED: new body is empty.")

    visible_new = strip_hidden(new)
    missing = [p for p in disclosure_paragraphs(old) if p not in visible_new]
    if missing:
        raise SystemExit("REFUSED: the existing disclosure text is missing, altered, or not visible "
                         "as prose in the new body (comments/fences don't count). Disclosure language "
                         "is never invented or edited by this tool — carry it over verbatim:\n\n"
                         + "\n\n".join(missing))

    diff = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                          "current-body", "new-body", lineterm=""))
    print(diff or "(bodies identical)")
    if old == new:
        raise SystemExit("REFUSED: new body is identical to the current body — nothing to apply.")
    if not a.yes:
        print(f"\n-- diff only; re-run with --yes to edit {a.repo}#{a.number}", file=sys.stderr)
        return 2

    # drift check: refuse if the body changed since this invocation's fetch
    live = _normalize(json.loads(run_gh(["pr", "view", str(a.number), "--repo", a.repo,
                                         "--json", "body"])).get("body"))
    if live != old:
        raise SystemExit("REFUSED: the PR body changed on GitHub since it was fetched — "
                         "re-run to review the current body (audit P2 TOCTOU).")

    # write-ahead: the intent row lands BEFORE the outward edit so a crash
    # between the two leaves a record of the attempt, never a silent edit
    base = {"repo": a.repo, "pr": a.number,
            "old_sha256": hashlib.sha256(old.encode()).hexdigest(),
            "new_sha256": hashlib.sha256(new.encode()).hexdigest(),
            "note": a.note.strip()}
    _append_ledger(a.ledger, {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "phase": "intent", **base})
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tf:
        tf.write(new)
        tmp = tf.name
    try:
        run_gh(["pr", "edit", str(a.number), "--repo", a.repo, "--body-file", tmp])
    finally:
        os.unlink(tmp)
    _append_ledger(a.ledger, {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "phase": "applied", **base})
    print(f"\napplied; amendment logged to {os.path.relpath(a.ledger, ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
