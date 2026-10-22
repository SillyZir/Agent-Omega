"""Localization benchmark — replay ground-truth issues through Omega's
localization stage (search + file ranking) and score file-match against
the files the real human fix touched.

For each issue in benchmark_issues.jsonl:
  1. clone the repo (shallow) into workspace/bench/, checkout fix_sha^
     (the repo state just before the human fix landed)
  2. run GitHubEnvironment._search_context + _rank_target_files
     (the exact production code path)
  3. compare predicted files against the human fix's files

Usage: python3 scripts/benchmark_localization.py [--limit N]
Output: benchmark_localization_results.jsonl + summary to stdout.
"""

import json
import os
import shutil
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from environments.github_env import GitHubEnvironment  # noqa: E402
from runner import env_guard as eg  # noqa: E402

BENCH_FILE = os.path.join(_ROOT, "benchmark_issues.jsonl")
OUT_FILE = os.path.join(_ROOT, "benchmark_localization_results.jsonl")
BENCH_WORKSPACE = os.path.join(_ROOT, "workspace", "bench")

# Fix files that don't count as the "core" fix location
_NONCORE_PATTERNS = ("_test.", ".test.", "/test", "test_", "/changelog",
                     "changelog/", ".changes/", ".md", ".yaml", ".yml",
                     ".txt", "/docs/")


def _run(cmd, cwd=None, timeout=600):
    return subprocess.run(cmd, cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _checkout_path(safe):
    """Where this repo's benchmark clone belongs.

    Rust repos are homed on the APFS image (P1, 2026-09-04): exFAT has no hard
    links, so cargo's incremental cache degrades to copying, and no native
    xattrs, so macOS writes `._*` sidecars that build scripts pick up as
    sources. Everything else keeps the historical bench workspace unchanged.

    A repo the registry calls Rust must never fall back to exFAT just because
    the image happens to be detached, so that case raises.

    SCOPE OF THAT RAISE — it prevents a WRONG-FILESYSTEM run, not a wasted one.
    `phase_x2_run.py` calls prepare_checkout unguarded (:282) inside a
    `except Exception` that writes a `runner_crash` record (:462), and the
    resume path treats any recorded issue as done. So under that runner a
    detached image still CONSUMES the corpus issue — exactly as the
    pre-existing `checkout_failed` path does. It is not fail-closed with
    respect to corpus consumption, and nothing at this layer can make it so.
    Call `env_guard.require_rustcache()` before a run starts; that is the only
    place the precondition can be enforced without spending anything.
    """
    if not eg.is_rust_repo(safe):
        return os.path.join(BENCH_WORKSPACE, safe)
    err = eg.check_rustcache()
    if err:
        raise eg.StorageError(
            f"{safe} is a Rust benchmark repo and must be checked out on the "
            f"APFS image, but: {err}")
    return eg.rust_bench_dir(safe)


def _rehome_if_rust(path, safe):
    """First sight of a repo: if it is a Cargo project, move it to the image.

    Language is not known before the clone, so a new repo lands in the bench
    workspace and is relocated once, before any cargo build runs against it.
    Thereafter `_checkout_path` routes it directly and this never runs again.
    """
    if eg.is_rust_repo(safe) or not os.path.exists(
            os.path.join(path, "Cargo.toml")):
        return path
    err = eg.check_rustcache()
    if err:
        raise eg.StorageError(
            f"{safe} is a Cargo project and must be homed on the APFS image, "
            f"but: {err}")
    dest = eg.rust_bench_dir(safe)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.isdir(os.path.join(dest, ".git")):
        # A checkout is already on the image, so what was missing is the
        # REGISTRY entry, not the tree. Adopt the existing checkout and drop
        # the staging clone. Deleting it to make room for a fresh shallow
        # clone would silently destroy a good tree (and any local state) to
        # recover from a bookkeeping gap.
        print(f"  {safe}: already on {eg.RUSTCACHE_VOLUME}, adopting it")
        shutil.rmtree(path, ignore_errors=True)
        eg.mark_rust_repo(safe)
        return dest
    print(f"  {safe}: Cargo project — rehoming onto {eg.RUSTCACHE_VOLUME}")
    if os.path.exists(dest):
        shutil.rmtree(dest)     # remnant of an interrupted move; no .git
    shutil.move(path, dest)
    eg.mark_rust_repo(safe)
    return dest


def prepare_checkout(repo, fix_sha):
    """Clone (cached) and checkout the parent of the fix commit.

    Returns the clone path, or None on failure.
    """
    safe = repo.replace("/", "__")
    path = _checkout_path(safe)
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = _run(["git", "clone", "--depth", "1", "--quiet",
                  f"https://github.com/{repo}.git", path])
        if r.returncode != 0:
            print(f"  clone failed: {r.stderr.strip()[:120]}")
            return None
        path = _rehome_if_rust(path, safe)
    # Fetch the fix commit with one parent, then check out the parent
    r = _run(["git", "fetch", "--depth", "2", "--quiet", "origin", fix_sha],
             cwd=path)
    if r.returncode != 0:
        print(f"  fetch {fix_sha[:10]} failed: {r.stderr.strip()[:120]}")
        return None
    r = _run(["git", "checkout", "-f", "--quiet", f"{fix_sha}^"], cwd=path)
    if r.returncode != 0:
        print(f"  checkout {fix_sha[:10]}^ failed: {r.stderr.strip()[:120]}")
        return None
    # Remove untracked leftovers from previous checkouts of other SHAs so
    # one issue's tree can't contaminate another's search/exploration
    _run(["git", "clean", "-fdq"], cwd=path)
    # AppleDouble junk (exFAT volume): git-ignored so clean skips it, but
    # it pollutes Glob results and wastes agent exploration turns.
    # Rust-homed checkouts are on APFS, which stores xattrs natively and never
    # creates a sidecar, so the sweep is skipped rather than walking the tree
    # every checkout to find nothing (P1).
    if not eg.is_rust_repo(safe):
        _run(["find", path, "-name", "._*", "-delete"])
    return path


def core_files(fix_files):
    core = [f for f in fix_files
            if not any(p in f.lower() for p in _NONCORE_PATTERNS)]
    return core or fix_files  # test-only fixes: fall back to all


def score(predicted, fix_files):
    """Score predictions against ground truth fix files."""
    core = core_files(fix_files)
    core_dirs = {os.path.dirname(f) for f in core}
    top1 = predicted[0] if predicted else ""
    return {
        "top1_exact": top1 in fix_files,
        "top1_core": top1 in core,
        "top1_dir": os.path.dirname(top1) in core_dirs if top1 else False,
        "top3_exact": any(p in fix_files for p in predicted[:3]),
        "top3_core": any(p in core for p in predicted[:3]),
    }


def main():
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    issues = [json.loads(l) for l in open(BENCH_FILE)]
    if limit:
        issues = issues[:limit]
    print(f"benchmarking localization on {len(issues)} issues", flush=True)

    env = GitHubEnvironment(target_repos=[])
    results = []
    with open(OUT_FILE, "w") as out:
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
                env.clone_dir = path
                env.current_repo = iss["repo"]
                try:
                    search_results = env._search_context(
                        {"title": iss["title"], "body": iss["body"]})
                    ranked, viable, logs = env._rank_target_files(search_results)
                except Exception as e:
                    rec["error"] = f"{type(e).__name__}: {str(e)[:120]}"
                    ranked, viable = [], []
                if "error" not in rec:
                    rec["predicted"] = ranked
                    rec["search_method"] = getattr(env, "_last_search_method", "")
                    rec["search_hits"] = len(search_results)
                    rec.update(score(ranked, iss["fix_files"]))
            rec["seconds"] = round(time.time() - t0, 1)
            out.write(json.dumps(rec) + "\n")
            out.flush()
            results.append(rec)

    summarize(results)


def summarize(results, title="LOCALIZATION BASELINE"):
    ok = [r for r in results if "error" not in r]
    errs = [r for r in results if "error" in r]
    print("\n" + "=" * 64)
    print(f"{title}  ({len(ok)} scored, {len(errs)} errors)")
    print("=" * 64)
    if errs:
        for r in errs:
            print(f"  ERROR {r['issue_key']}: {r['error']}")
    if not ok:
        return
    for metric in ("top1_exact", "top1_core", "top1_dir",
                   "top3_exact", "top3_core"):
        n = sum(1 for r in ok if r.get(metric))
        print(f"  {metric:12s} {n:2d}/{len(ok)}  ({n/len(ok):.0%})")
    no_pred = sum(1 for r in ok if not r.get("predicted"))
    print(f"  no prediction: {no_pred}/{len(ok)}")
    by_method = {}
    for r in ok:
        by_method.setdefault(r.get("search_method", "?"), []).append(r)
    for m, rs in sorted(by_method.items()):
        n = sum(1 for r in rs if r.get("top1_core"))
        print(f"  method={m:12s} n={len(rs):2d}  top1_core={n}/{len(rs)}")


if __name__ == "__main__":
    sys.exit(main())
