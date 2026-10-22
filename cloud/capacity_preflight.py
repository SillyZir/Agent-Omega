"""Capacity preflight for the 09:17 chain — the in-25 closure.

Gates on exactly one question: can every storage volume the chain depends on
actually WRITE today? Answered the only way this topology allows — write real
bytes, fsync, verify the size, delete, verify the deletion.

Deliberately NOT a df/diskutil threshold. During in-25 the two disagreed with
each other and were both wrong: `df` on the sparsebundle guest showed 0 avail
against 8.6 G used (impossible), `diskutil` showed 205 GB container free while
a 50 MB write failed instantly. The probe is the entire gate; `df` numbers are
logged as diagnostic context only.

Probe sizes: 64 MiB on the data volumes — the sparsebundle allocates 8 MiB
bands lazily, so 64 MiB forces ~8 real band allocations through to the exFAT
host, which is exactly the path that silently vanished in in-25. 8 MiB on the
repo root (boot disk), whose failure would break the reporting layer itself.

Exit codes: 0 all targets writable; 3 any probe failed (distinct from every
chain-stage code so launchd status and the summary line identify the preflight
unambiguously).

    python3 cloud/capacity_preflight.py                     # production targets
    python3 cloud/capacity_preflight.py --target PATH SIZE  # test override
"""

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MIB = 1024 * 1024
EXIT_FAIL = 3
LOG = os.path.join(ROOT, "logs", "monitor", "preflight.log")


def production_targets():
    # env_guard is the single source of truth for where the chain's storage
    # lives; import inside so --target test runs never require it.
    from runner import env_guard
    return [
        (env_guard.GOMODCACHE, 64 * MIB),          # in-25 casualty (sparsebundle)
        (os.path.realpath(os.path.join(ROOT, "workspace")), 64 * MIB),  # clones
        (ROOT, 8 * MIB),                           # logs/summaries/git (boot)
    ]


def df_line(path):
    """Diagnostic context ONLY — never gates (in-25: df lies here)."""
    try:
        r = subprocess.run(["df", "-h", path], capture_output=True,
                           encoding="utf-8", timeout=10)
        lines = (r.stdout or "").strip().splitlines()
        return lines[-1] if len(lines) > 1 else "df: no output"
    except Exception as e:  # noqa: BLE001 — diagnostics must never crash the gate
        return f"df: {type(e).__name__}"


def probe(target, size):
    """Write, fsync, stat, delete, verify gone. Returns (ok, detail)."""
    if not os.path.isdir(target):
        return False, "target directory does not exist (volume not mounted?)"
    path = os.path.join(target, f".omega_preflight_probe.{os.getpid()}")
    block = b"\0" * MIB
    try:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as e:
            return False, f"create failed: {e}"
        try:
            with os.fdopen(fd, "wb") as fh:
                for _ in range(size // MIB):
                    fh.write(block)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as e:
            return False, f"write/fsync failed: {e}"
        try:
            actual = os.stat(path).st_size
        except OSError as e:
            return False, f"stat after write failed: {e}"
        if actual != size:
            return False, f"short write: {actual} of {size} bytes"
        return True, f"{size // MIB} MiB written+fsynced+removed"
    finally:
        # Unconditional: a failed probe must not leave debris, and a leftover
        # probe file must itself fail the preflight rather than pass silently.
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    # unreachable


def run(targets):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    failures = []
    for target, size in targets:
        ok, detail = probe(target, size)
        # verify the delete actually happened — a probe we cannot remove is a
        # failure too (the operator's criterion is write AND remove)
        leftover = os.path.exists(
            os.path.join(target, f".omega_preflight_probe.{os.getpid()}"))
        if ok and leftover:
            ok, detail = False, "probe file could not be removed"
        status = "ok" if ok else "FAIL"
        print(f"[capacity_preflight] {status}  {target}  {detail}",
              file=sys.stderr)
        print(f"[capacity_preflight]      df: {df_line(target)}",
              file=sys.stderr)
        if not ok:
            failures.append((target, detail))

    line = (f"{stamp} " + ("PASS" if not failures else
            "FAIL " + "; ".join(f"{t}: {d}" for t, d in failures)))
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # boot-disk failure: stderr already carries the story

    if failures:
        print(f"[capacity_preflight] FAIL — {len(failures)} target(s) "
              "unwritable; the chain must not start", file=sys.stderr)
        return EXIT_FAIL
    print("[capacity_preflight] all targets writable", file=sys.stderr)
    return 0


def main(argv):
    if "--target" in argv:
        i = argv.index("--target")
        target, size = argv[i + 1], int(argv[i + 2]) * MIB
        return run([(target, size)])
    return run(production_targets())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
