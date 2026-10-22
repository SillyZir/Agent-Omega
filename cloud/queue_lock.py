"""Single-writer discipline for scout_queue.jsonl (system audit P1-1,
operator-directed fix 2026-09-01).

The queue has two writers with different styles: the scout APPENDS new
candidates; the executor REWRITES the whole file from its in-memory list.
In the daily chain they run sequentially, but a manual run overlapping the
chain could interleave — and a row appended between the executor's read
and its atomic replace was lost forever (the scout's seen-keys state
already marks the issue seen, so it would never re-queue).

An fcntl.flock around every queue mutation makes the writers take turns;
the executor additionally re-merges rows it has never seen at write time
(belt and braces — a lock bug degrades to the old race, not to silent
corruption). flock is advisory and blocking: mutations are milliseconds,
so a waiter stalls briefly instead of corrupting.
"""
import fcntl


class QueueLock:
    def __init__(self, queue_path):
        self._path = queue_path + ".lock"
        self._fh = None

    def __enter__(self):
        self._fh = open(self._path, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()
        return False
