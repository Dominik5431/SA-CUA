"""
Cross-process OwnCloud instance allocation using advisory file locks.

When two separate processes (e.g. two terminal runs) both need an OwnCloud
instance, the first one acquires a flock on lock-file 1 and gets port 8092;
the second finds lock-file 1 taken, falls through to lock-file 2, and gets
port 8093.  The OS releases the lock automatically when the process exits,
even on crash.

Usage:
    from owncloud_instance import acquire_instance, get_current_instance

    inst = acquire_instance()   # call once; subsequent calls return the same inst
    # inst = {"id": 1, "suffix": "", "port": 8092, "container": "owncloud"}
    # also sets os.environ["OWNCLOUD_INSTANCE_SUFFIX"] = inst["suffix"]
"""
import fcntl
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_LOCK_DIR = "/tmp"

_INSTANCES = [
    {"id": 1, "suffix": "",   "port": 8092, "container": "owncloud"},
    {"id": 2, "suffix": "-2", "port": 8093, "container": "owncloud-2"},
    {"id": 3, "suffix": "-3", "port": 8094, "container": "owncloud-3"},
]

_held_fd = None
_current_instance = None


def acquire_instance() -> dict:
    """Claim the first available OwnCloud instance via an exclusive flock.

    Idempotent: if this process already holds an instance, returns it immediately.
    Sets os.environ["OWNCLOUD_INSTANCE_SUFFIX"] as a side-effect so that
    subprocesses (e.g. reset.sh) inherit the right value.

    Raises RuntimeError if all instances are already locked by other processes.
    """
    global _held_fd, _current_instance

    if _current_instance is not None:
        return _current_instance

    _pin = os.environ.get("OWNCLOUD_INSTANCE_ID")
    _candidates = _INSTANCES
    if _pin:
        _candidates = [i for i in _INSTANCES if str(i["id"]) == _pin.strip()]
        if not _candidates:
            raise RuntimeError(
                f"OWNCLOUD_INSTANCE_ID={_pin!r} names no instance; valid ids are "
                + ", ".join(str(i["id"]) for i in _INSTANCES))

    for inst in _candidates:
        lock_path = os.path.join(_LOCK_DIR, f"owncloud_instance_{inst['id']}.lock")
        try:
            fd = open(lock_path, "w")
        except PermissionError:
            logger.debug("OwnCloud instance %d is unavailable (cannot open lock file), trying next.", inst["id"])
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            fd.close()
            logger.debug("OwnCloud instance %d is locked, trying next.", inst["id"])
            continue

        fd.write(str(os.getpid()))
        fd.flush()
        _held_fd = fd
        _current_instance = inst
        os.environ["OWNCLOUD_INSTANCE_SUFFIX"] = inst["suffix"]
        logger.info(
            "Acquired OwnCloud instance %d (port %d, container '%s').",
            inst["id"], inst["port"], inst["container"],
        )
        return inst

    raise RuntimeError(
        "No free OwnCloud instance available — all 3 are locked by other processes. "
        "Wait for a running evaluation to finish, or start fewer parallel runs."
    )


def get_current_instance() -> Optional[dict]:
    """Return the instance claimed by this process, or None if not yet acquired."""
    return _current_instance
