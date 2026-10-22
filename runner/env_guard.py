"""Storage / environment guard for the v2 production pipeline.

Single source of truth for *where bytes land*. Every heavy operation —
repo clones, Go module/build caches, cargo target dirs, rustup toolchains —
must live on the external AgentSSD (mounted via the repo's ``workspace``
symlink), never on the nearly-full internal boot disk.

Both the production pipeline (``runner/pipeline_v2.py``) and the Stage 1–3
benchmark scripts import their build environment from here, so there is
exactly one definition of the cache layout.

Hard-won constraints encoded here (see project_stage3_execution_validation):
  * ``~/go/pkg/mod`` is corrupted -> always use a dedicated GOMODCACHE.
  * that dedicated GOMODCACHE must be on a filesystem with enforced permission
    bits, i.e. the APFS image, NOT exFAT AgentSSD (in-24, 2026-08-05).
  * ``CARGO_TARGET_DIR`` must live OUTSIDE the clone (git clean would wipe
    in-clone target dirs between control/patched runs -> cold rebuilds).
  * rustup/cargo homed on the SSD; the internal disk has only a few GiB free.

Run standalone to print the resolved config and exit non-zero if the SSD
is not mounted::

    python3 runner/env_guard.py
"""

import json
import os
import shutil
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# workspace is a symlink -> /Volumes/AgentSSD/omega-workspace-v2
WORKSPACE = os.path.join(_ROOT, "workspace")
BENCH_WORKSPACE = os.path.join(WORKSPACE, "bench")    # benchmark clones + caches
REPOS_WORKSPACE = os.path.join(WORKSPACE, "repos")    # production HEAD checkouts
CACHE_ROOT = BENCH_WORKSPACE                          # all build caches live here

# --- Go module cache: dedicated APFS image, NOT AgentSSD (in-24) -------------
# AgentSSD is exFAT. exFAT stores no permission bits, so the read-only mode Go
# sets on extracted modules — the module cache's ONLY integrity protection — is
# silently unenforced there. Measured 2026-08-05: `chmod 444` on AgentSSD leaves
# a file writable and a subsequent write succeeds; on this image it is refused.
# The observed consequence was extracted module subdirectories going EMPTY,
# which surfaces as a compile error *inside the package under test* — i.e. it
# reads exactly like the patch being wrong, and that step feeds the `tests_pass`
# auto-ship gate. A false-negative machine, so the cache moved off exFAT.
#
# GOCACHE deliberately stays on AgentSSD: Go hashes every build-cache entry on
# read, so damage there degrades to a cache miss and a rebuild. It is noisy,
# not silent, and does not need this protection.
GOMODCACHE_IMAGE = "/Volumes/AgentSSD/omega-gomodcache.sparsebundle"
GOMODCACHE_VOLUME = "/Volumes/OmegaGoCache"
GOMODCACHE = os.path.join(GOMODCACHE_VOLUME, "gomodcache")
GOMODCACHE_FSTYPE = "apfs"

# --- Rust workspace: dedicated APFS image, NOT AgentSSD (P1, 2026-09-04) -----
# Same reasoning as GOMODCACHE, different mechanism. exFAT breaks Rust two ways,
# both measured in the Phase-X2 records:
#
#   * No hard links. Cargo's incremental cache hard-links artifacts between
#     sessions and falls back to copying when it cannot — 14 of 16 Phase-X2
#     records carry "hard linking files in the incremental compilation cache
#     failed", i.e. every build paid for it.
#   * No native extended attributes. macOS emulates them with AppleDouble `._*`
#     sidecars written beside the real files; 876,843 accumulated under the
#     bench workspace. These are NOT inert. A build script that globs a source
#     directory picks them up as sources: `nushell/nushell#18682`'s control arm
#     died feeding `libgit2/src/util/unix/._realpath.c` to cc, which reads like
#     the repository being broken rather than the filesystem.
#
# APFS stores xattrs natively, so no sidecar is ever created — the class is
# eliminated structurally, not swept up. Rust checkouts, CARGO_HOME, RUSTUP_HOME
# and every CARGO_TARGET_DIR therefore live on this image.
#
# GOCACHE deliberately stays on AgentSSD (see above); this does not change that.
RUSTCACHE_IMAGE = "/Volumes/AgentSSD/omega-rust.sparsebundle"
RUSTCACHE_VOLUME = "/Volumes/OmegaRust"
RUSTCACHE_FSTYPE = "apfs"
RUST_BENCH_WORKSPACE = os.path.join(RUSTCACHE_VOLUME, "bench")

# Prepended so the pinned rustup shim is found before any system toolchain
_RUSTUP_BIN = "/opt/homebrew/opt/rustup/bin"


class StorageError(RuntimeError):
    """Raised when the SSD workspace is not mounted / not external."""


def cargo_target_dir(repo_id):
    """Per-repo cargo target dir, kept outside the clone so git clean can't
    delete it between control and patched runs.

    On the APFS Rust image since P1 (2026-09-04): cargo hard-links into this
    directory, which exFAT cannot do."""
    return os.path.join(RUSTCACHE_VOLUME, ".cargo-target", repo_id)


def rust_bench_dir(repo_id):
    """Checkout path for a Rust benchmark repo, on the APFS image (P1)."""
    return os.path.join(RUST_BENCH_WORKSPACE, repo_id)


# Which benchmark repos are Rust-homed, i.e. must be checked out on the APFS
# image rather than the exFAT bench workspace.
#
# This lives in the git repo, NOT on the image, on purpose: it has to be
# readable when the image is DETACHED. That is precisely the case where a
# path-existence test would answer "no Rust checkout here" and silently route a
# Rust repo back onto exFAT — reintroducing the sidecar and hard-link faults
# P1 exists to eliminate, invisibly. With the registry off-image, a detached
# image is a loud StorageError instead.
RUST_REPOS_FILE = os.path.join(_ROOT, "runner", "rust_bench_repos.json")


def rust_repos():
    """The set of repo ids (`owner__name`) homed on the Rust image."""
    try:
        with open(RUST_REPOS_FILE, encoding="utf-8") as fh:
            return set(json.load(fh).get("repos", []))
    except (OSError, ValueError):
        return set()


def is_rust_repo(repo_id):
    return repo_id in rust_repos()


def mark_rust_repo(repo_id):
    """Record a repo as Rust-homed. Idempotent; sorted for a readable diff."""
    repos = rust_repos()
    if repo_id in repos:
        return False
    repos.add(repo_id)
    tmp = RUST_REPOS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"note": "Repos checked out on the APFS Rust image (P1, "
                           "2026-09-04). Maintained by "
                           "scripts/benchmark_localization.py.",
                   "repos": sorted(repos)}, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, RUST_REPOS_FILE)
    return True


def build_env(extra=None):
    """The canonical build environment. Identical to the dict that produced
    the Stage 3 validation results; reused everywhere so caches never default
    back to the internal disk."""
    env = {
        **os.environ,
        "GOMODCACHE": GOMODCACHE,
        "GOCACHE": os.path.join(CACHE_ROOT, ".gocache"),
        # Rust state on the APFS image (P1): the registry and toolchain
        # directories are themselves hard-link-heavy and xattr-bearing.
        "CARGO_HOME": os.path.join(RUSTCACHE_VOLUME, ".cargo"),
        "RUSTUP_HOME": os.path.join(RUSTCACHE_VOLUME, ".rustup"),
        "PATH": _RUSTUP_BIN + ":" + os.environ.get("PATH", ""),
    }
    if extra:
        env.update(extra)
    return env


def _fstype(path):
    """Filesystem type of the volume backing `path`, or None if it cannot be
    determined. Parses mount(8) — in the base system, needs no privileges, and
    unlike `diskutil` it is not a per-volume lookup. Returning None on any
    failure is deliberate: callers treat "unknown" as non-fatal and rely on the
    device-identity check, which needs no subprocess at all."""
    try:
        out = subprocess.run(["/sbin/mount"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    target = os.path.realpath(path)
    best = None
    for line in out.splitlines():
        # "/dev/disk5s1 on /Volumes/OmegaGoCache (apfs, local, nosuid, ...)"
        if " on " not in line or "(" not in line:
            continue
        mount_point = line.split(" on ", 1)[1].rsplit(" (", 1)[0]
        opts = line.rsplit("(", 1)[1].rstrip(")")
        if target == mount_point or target.startswith(mount_point.rstrip("/") + "/"):
            # longest matching mount point wins, so /Volumes/X beats /
            if best is None or len(mount_point) > len(best[0]):
                best = (mount_point, opts.split(",")[0].strip())
    return best[1] if best else None


def check_gomodcache():
    """Validate that GOMODCACHE really is the mounted APFS image.

    Returns None when healthy, else a string describing the failure.

    This guards one specific silent-failure mode. If the image is detached but
    AgentSSD is mounted, ``GOMODCACHE_VOLUME`` is just a missing directory in a
    writable location, and the Go toolchain would happily CREATE it and
    repopulate the whole cache onto a filesystem that cannot protect it. Nothing
    would fail at that moment; the damage would surface days later as a build
    error attributed to a patch. So every check below is by device identity
    rather than path existence — a path that merely *looks* right is precisely
    the failure being guarded against — and nothing here creates a directory.
    """
    if not os.path.isdir(GOMODCACHE_VOLUME):
        return (f"Go module-cache image is not attached: {GOMODCACHE_VOLUME} does not "
                f"exist. Attach it with `scripts/mount_gomodcache.sh`. Refusing to run "
                f"— the toolchain would otherwise rebuild the cache on exFAT (in-24).")
    parent = os.path.dirname(GOMODCACHE_VOLUME.rstrip("/")) or "/"
    try:
        volume_dev = os.stat(GOMODCACHE_VOLUME).st_dev
        parent_dev = os.stat(parent).st_dev
    except OSError as e:
        return f"cannot stat module-cache volume {GOMODCACHE_VOLUME}: {e}"
    if volume_dev == parent_dev:
        return (f"Go module-cache image is not attached: {GOMODCACHE_VOLUME} is a plain "
                f"directory on {parent}, not a mounted volume. Refusing to use it as a "
                f"module cache. Attach the image with `scripts/mount_gomodcache.sh`.")
    fstype = _fstype(GOMODCACHE_VOLUME)
    if fstype is not None and fstype.lower() != GOMODCACHE_FSTYPE:
        return (f"module-cache volume {GOMODCACHE_VOLUME} is {fstype!r}, expected "
                f"{GOMODCACHE_FSTYPE!r}. A filesystem without enforced permission bits "
                f"cannot protect the module cache (in-24).")
    if not os.path.isdir(GOMODCACHE):
        return (f"module cache {GOMODCACHE} is missing from the attached image. Refusing "
                f"to create it implicitly; run `scripts/mount_gomodcache.sh --init`.")
    try:
        cache_dev = os.stat(GOMODCACHE).st_dev
    except OSError as e:
        return f"cannot stat module cache {GOMODCACHE}: {e}"
    if cache_dev != volume_dev:
        return (f"module cache {GOMODCACHE} is not on the attached image "
                f"(device {cache_dev} != volume {volume_dev}) — refusing to use it.")
    return None


def check_rustcache():
    """Validate that the Rust workspace really is the mounted APFS image.

    Returns None when healthy, else a string describing the failure.

    Same silent-failure shape as ``check_gomodcache``, so the same discipline:
    every check is by *device identity*, not path existence, and nothing here
    creates a directory. If the image is detached, ``/Volumes/OmegaRust`` is an
    absent path inside a writable location, and cargo would cheerfully create it
    — on the boot disk, which has ~12 GiB free — and rebuild everything there.
    Nothing would fail at that moment.

    Deliberately NOT wired into ``resolve()``. The Go production pipeline does
    not touch Rust, and making the daily chain fail closed on an image it never
    reads would be a self-inflicted outage. Rust entrypoints call
    ``require_rustcache()`` instead.
    """
    if not os.path.isdir(RUSTCACHE_VOLUME):
        return (f"Rust workspace image is not attached: {RUSTCACHE_VOLUME} does not "
                f"exist. Attach it with `scripts/mount_rustcache.sh`. Refusing to run "
                f"— cargo would otherwise rebuild onto exFAT or the boot disk (P1).")
    parent = os.path.dirname(RUSTCACHE_VOLUME.rstrip("/")) or "/"
    try:
        volume_dev = os.stat(RUSTCACHE_VOLUME).st_dev
        parent_dev = os.stat(parent).st_dev
    except OSError as e:
        return f"cannot stat Rust workspace volume {RUSTCACHE_VOLUME}: {e}"
    if volume_dev == parent_dev:
        return (f"Rust workspace image is not attached: {RUSTCACHE_VOLUME} is a plain "
                f"directory on {parent}, not a mounted volume. Refusing to use it. "
                f"Attach the image with `scripts/mount_rustcache.sh`.")
    fstype = _fstype(RUSTCACHE_VOLUME)
    if fstype is not None and fstype.lower() != RUSTCACHE_FSTYPE:
        return (f"Rust workspace volume {RUSTCACHE_VOLUME} is {fstype!r}, expected "
                f"{RUSTCACHE_FSTYPE!r}. A filesystem without hard links and native "
                f"extended attributes corrupts cargo's incremental cache and pollutes "
                f"source trees with AppleDouble `._*` files (P1).")
    if not os.path.isdir(RUST_BENCH_WORKSPACE):
        return (f"Rust bench workspace {RUST_BENCH_WORKSPACE} is missing from the "
                f"attached image. Refusing to create it implicitly; run "
                f"`scripts/mount_rustcache.sh --init`.")
    try:
        bench_dev = os.stat(RUST_BENCH_WORKSPACE).st_dev
    except OSError as e:
        return f"cannot stat Rust bench workspace {RUST_BENCH_WORKSPACE}: {e}"
    if bench_dev != volume_dev:
        return (f"Rust bench workspace {RUST_BENCH_WORKSPACE} is not on the attached "
                f"image (device {bench_dev} != volume {volume_dev}) — refusing to use it.")
    return None


def require_rustcache():
    """Assert the Rust APFS image is attached. Raises StorageError otherwise.

    Call from every Rust entrypoint, before any cargo invocation."""
    err = check_rustcache()
    if err:
        raise StorageError(err)
    return build_env()


def rust_env_facts(scan_path=None):
    """Raw, uninterpreted facts about the filesystem a Rust run is using.

    Recorded alongside control-arm forensics so a failure can be attributed to
    the storage layer *after the fact* instead of guessed at. Deliberately
    factual: filesystem type, whether hard links actually work, how many
    AppleDouble sidecars are present. It draws no conclusion and gates nothing —
    per the R8/A3-1 ruling there is no environment-marker vocabulary and no
    analysis-time classifier. These are measurements, not verdicts.
    """
    scan_path = scan_path or RUSTCACHE_VOLUME
    facts = {"volume": RUSTCACHE_VOLUME, "scan_path": scan_path,
             "fstype": _fstype(scan_path), "hardlinks": None,
             "appledouble_files": None,
             "free_gib": round(_free_gib(RUSTCACHE_VOLUME), 2)}
    # The hard-link probe writes at the VOLUME root, never inside scan_path —
    # scan_path is usually a git checkout and a stray probe file would dirty it.
    probe = os.path.join(RUSTCACHE_VOLUME, ".omega_hardlink_probe")
    link = probe + ".link"
    try:
        with open(probe, "w") as fh:
            fh.write("probe")
        try:
            os.link(probe, link)
            facts["hardlinks"] = True
        except OSError:
            facts["hardlinks"] = False
    except OSError:
        pass
    finally:
        for f in (link, probe):
            try:
                os.remove(f)
            except OSError:
                pass
    try:
        n = 0
        for _root, _dirs, files in os.walk(scan_path):
            n += sum(1 for f in files if f.startswith("._"))
            if n > 10000:      # bounded: "many" is as actionable as an exact count
                break
        facts["appledouble_files"] = n
    except OSError:
        pass
    return facts


def resolve():
    """Resolve and validate the storage configuration.

    Returns a dict of resolved paths. Raises StorageError if the workspace
    symlink is dangling (SSD unmounted) or resolves onto the boot disk.
    """
    real = os.path.realpath(WORKSPACE)
    if not os.path.isdir(real):
        raise StorageError(
            f"workspace not mounted: {WORKSPACE} -> {real} (is AgentSSD plugged in?)")
    # The whole point is to stay off the boot disk: refuse if the resolved
    # workspace shares a device with "/".
    if os.stat(real).st_dev == os.stat("/").st_dev:
        raise StorageError(
            f"workspace resolves onto the internal/boot disk ({real}); "
            "refusing to run heavy operations there")
    # Fail closed if the module cache is not the attached APFS image. Checked
    # here rather than in require() so benchmark scripts, which call resolve()
    # directly, get the same protection as the production entrypoints.
    gomodcache_error = check_gomodcache()
    if gomodcache_error:
        raise StorageError(gomodcache_error)
    return {
        "workspace": WORKSPACE,
        "workspace_real": real,
        "bench_clones": BENCH_WORKSPACE,
        "repos": REPOS_WORKSPACE,
        "cache_root": CACHE_ROOT,
        "gomodcache": GOMODCACHE,
        "gocache": os.path.join(CACHE_ROOT, ".gocache"),
        "cargo_home": os.path.join(RUSTCACHE_VOLUME, ".cargo"),
        "rustup_home": os.path.join(RUSTCACHE_VOLUME, ".rustup"),
        "cargo_target_root": os.path.join(RUSTCACHE_VOLUME, ".cargo-target"),
        "rust_bench": RUST_BENCH_WORKSPACE,
        # Reported, not enforced: Go entrypoints must not fail on the Rust image.
        "rustcache_attached": check_rustcache() is None,
    }


def _free_gib(path):
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except OSError:
        return float("nan")


def report(cfg=None, stream=sys.stderr):
    """Print the resolved storage config + free space for both disks."""
    cfg = cfg or resolve()
    real = cfg["workspace_real"]
    lines = [
        "── storage configuration (v2) ─────────────────────────────",
        f"  workspace        {cfg['workspace']} -> {real}",
        f"  benchmark clones {cfg['bench_clones']}",
        f"  prod checkouts   {cfg['repos']}",
        f"  GOMODCACHE       {cfg['gomodcache']}  [APFS image, {_fstype(GOMODCACHE_VOLUME)}]",
        f"  GOCACHE          {cfg['gocache']}",
        f"  rust checkouts   {cfg['rust_bench']}"
        f"  [APFS image, {_fstype(RUSTCACHE_VOLUME)}"
        f"{'' if cfg['rustcache_attached'] else ', NOT ATTACHED'}]",
        f"  CARGO_HOME       {cfg['cargo_home']}",
        f"  RUSTUP_HOME      {cfg['rustup_home']}",
        f"  CARGO_TARGET_DIR {cfg['cargo_target_root']}/<repo>",
        f"  free: SSD {_free_gib(real):.1f} GiB   internal / {_free_gib('/'):.1f} GiB",
        "───────────────────────────────────────────────────────────",
    ]
    print("\n".join(lines), file=stream, flush=True)
    return cfg


def require(verbose=True):
    """Assert the SSD is mounted, optionally print the report, and return the
    build environment ready to hand to subprocesses. Call this first from
    every production entrypoint."""
    cfg = resolve()
    if verbose:
        report(cfg)
    os.makedirs(cfg["bench_clones"], exist_ok=True)
    os.makedirs(cfg["repos"], exist_ok=True)
    return build_env()


if __name__ == "__main__":
    try:
        report()
    except StorageError as e:
        print(f"[STORAGE_ABORT] {e}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
