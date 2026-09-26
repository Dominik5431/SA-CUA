"""
Cross-process Reddit (postmill `forum`) instance allocation via advisory file locks.

Reddit used to be a single shared `forum` container, so two reddit runs at once
clobbered each other: every episode does a row-level UPDATE of the target post/comment
and, when a populate spec is present, a full RESET_REDDIT_DATABASE re-seed -- run two of
those against one postgres and each overwrites the other's in-flight injected content and
can reset the DB mid-episode, corrupting BOTH runs' adversarial numbers.

The fix mirrors owncloud_instance.py / rocketchat_instance.py: stand up forum copies
(forum / forum-2 / forum-3 / forum-4, published on host ports 9999 / 9998 / 9997 / 9996 -- these MUST match
WEB_PLATFORM_ROUTES["reddit"] in desktop_env.py, which is what the web proxy forwards each
slot to) and hand each run its own. The chosen container name is exported as
REDDIT_CONTAINER so adv/WebArena_Reddit/injection.py targets the right postgres, and the
slot id drives the proxy route so the agent's browser reaches the matching web frontend.

Bring the extra containers up once. Most of the corpus is baked into the image layers, but
the WASP-reddit seed post (submissions.id 137403, /f/allentown/137403) is NOT -- it was a
one-time insert into the original `forum` container during convert_wasp_examples.py, so a
fresh pool container 404s on it and every wasp_reddit episode scores benign 0 with an
understated ASR. Seed it explicitly after `docker run` (idempotent):

    docker run -d --name forum-2 -p 9998:80 postmill-populated-exposed-withimg
    docker run -d --name forum-3 -p 9997:80 postmill-populated-exposed-withimg
    docker run -d --name forum-4 -p 9996:80 postmill-populated-exposed-withimg
    bash seed_wasp_reddit_post.sh forum-2 forum-3 forum-4   # <-- required for wasp_reddit

Usage:
    from reddit_instance import acquire_instance, get_current_instance

    inst = acquire_instance()   # call once; subsequent calls return the same inst
    # inst = {"id": 1, "port": 9999, "container": "forum"}
    # also sets os.environ["REDDIT_CONTAINER"] = inst["container"]

Pin a specific slot with REDDIT_INSTANCE_ID (e.g. to run one suite on forum-2 alongside a
different suite already using forum): the pin still goes through the same flock, so a busy
slot raises instead of being stolen from the run that holds it.
"""
import fcntl
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_LOCK_DIR = "/tmp"

_INSTANCES = [
    {"id": 1, "port": 9999, "container": "forum"},
    {"id": 2, "port": 9998, "container": "forum-2"},
    {"id": 3, "port": 9997, "container": "forum-3"},
    {"id": 4, "port": 9996, "container": "forum-4"},
]

_held_fd = None
_current_instance = None


def acquire_instance() -> dict:
    """Claim the first available Reddit instance via an exclusive flock.

    Idempotent: if this process already holds an instance, returns it immediately.
    Sets os.environ["REDDIT_CONTAINER"] as a side-effect so the injection code
    (and any subprocess it spawns) targets the right postgres container.

    Honours REDDIT_INSTANCE_ID to pin a specific slot; the pin is still taken under
    flock, so a slot held by another process raises rather than being stolen.

    Raises RuntimeError if no instance (or the pinned one) is available.
    """
    global _held_fd, _current_instance

    if _current_instance is not None:
        return _current_instance

    _pin = os.environ.get("REDDIT_INSTANCE_ID")
    _candidates = _INSTANCES
    if _pin:
        _candidates = [i for i in _INSTANCES if str(i["id"]) == _pin.strip()]
        if not _candidates:
            raise RuntimeError(
                f"REDDIT_INSTANCE_ID={_pin!r} names no instance; valid ids are "
                + ", ".join(str(i["id"]) for i in _INSTANCES))

    for inst in _candidates:
        lock_path = os.path.join(_LOCK_DIR, f"reddit_instance_{inst['id']}.lock")
        try:
            fd = open(lock_path, "w")
        except PermissionError:
            logger.debug("Reddit instance %d unavailable (cannot open lock file), trying next.", inst["id"])
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            fd.close()
            logger.debug("Reddit instance %d is locked, trying next.", inst["id"])
            continue

        fd.write(str(os.getpid()))
        fd.flush()
        _held_fd = fd
        _current_instance = inst
        os.environ["REDDIT_CONTAINER"] = inst["container"]
        logger.info(
            "Acquired Reddit instance %d (port %d, container '%s').",
            inst["id"], inst["port"], inst["container"],
        )
        return inst

    raise RuntimeError(
        "No free Reddit instance available — all are locked by other processes. "
        "Wait for a running evaluation to finish, or start fewer parallel runs."
    )


def get_current_instance() -> Optional[dict]:
    """Return the instance claimed by this process, or None if not yet acquired."""
    return _current_instance
