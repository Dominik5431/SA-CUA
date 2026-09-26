#!/usr/bin/env python3
"""
manifest_io.py -- concurrency-safe merge into at-spi/manifest.json.

Both capture scripts used to do the same read-modify-write: load the whole manifest into a dict at
startup, then rewrite the WHOLE FILE from that in-memory snapshot after every capture. With one
run that is fine. With two -- a web pass and an app pass, or a consent-variant pass overlapping
either -- it silently destroys rows: the second run's snapshot predates the first run's later
captures, so every write rolls them back, and whichever process finishes last wins. Nothing errors,
nothing warns, and the loss is only visible as captures on disk with no manifest row.

So a writer here never writes rows it did not produce. It takes a lock, re-reads whatever is on
disk *now*, applies only its own rows, and replaces the file atomically.
"""
import json
import os

from filelock import FileLock

_LOCK_TIMEOUT = 120


def merge_rows(manifest_path: str, rows_by_index: dict) -> None:
    """Merge `rows_by_index` ({index: row}) into manifest_path, under a lock.

    Only the caller's own indices are touched; every other row on disk is preserved as found, which
    is what makes two concurrent capture runs safe. The write is tmp + os.replace, so a reader
    never observes a half-written manifest.
    """
    if not rows_by_index:
        return
    lock = FileLock(manifest_path + ".lock", timeout=_LOCK_TIMEOUT)
    with lock:
        current: dict = {}
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    current = {row["index"]: row for row in json.load(f)}
            except (ValueError, KeyError):
                current = {}
        current.update(rows_by_index)
        tmp = manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([current[i] for i in sorted(current)], f, indent=2)
        os.replace(tmp, manifest_path)


def remove_indices(manifest_path: str, indices) -> int:
    """Delete rows by index, under the same lock. Returns how many were removed.

    Separate from merge_rows() because that one only ever adds or updates: a writer must not be
    able to delete another run's rows by omission, which is exactly the bug this module exists to
    stop. Removal is therefore always explicit and always by index.
    """
    indices = set(indices)
    if not indices:
        return 0
    lock = FileLock(manifest_path + ".lock", timeout=_LOCK_TIMEOUT)
    with lock:
        if not os.path.exists(manifest_path):
            return 0
        with open(manifest_path, encoding="utf-8") as f:
            rows = json.load(f)
        keep = [r for r in rows if r["index"] not in indices]
        removed = len(rows) - len(keep)
        tmp = manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(keep, f, indent=2)
        os.replace(tmp, manifest_path)
    return removed


def max_index(manifest_path: str) -> int:
    """Highest index currently on disk, or -1. Read under the same lock, so it cannot observe a
    partial write -- but note it is still a snapshot: a concurrent run may claim indices after it,
    which is why an overlapping run should be given an explicit --start_index."""
    lock = FileLock(manifest_path + ".lock", timeout=_LOCK_TIMEOUT)
    with lock:
        if not os.path.exists(manifest_path):
            return -1
        try:
            with open(manifest_path, encoding="utf-8") as f:
                rows = json.load(f)
        except (ValueError, KeyError):
            return -1
    return max((r["index"] for r in rows), default=-1)
