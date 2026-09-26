"""
Cross-process GitLab instance allocation via advisory file locks.

GitLab used to be one shared `gitlab` container, so every GitLab suite (wasp_effort_gitlab,
wasp_gitlab_value, ...) of every agent/seed serialized on it: each episode files a fresh
attacker issue, deletes the older ones, resets members/branches/.zshrc through the REST API
-- two runs at once would reset each other's scenario mid-episode.

The fix mirrors reddit_instance.py: a second copy of the populated image, each run holding
one via flock. Ports must match WEB_PLATFORM_ROUTES["gitlab"] in desktop_env.py (what the
web proxy forwards each slot to). 8623/8624 sit outside every docker-VM port-allocation window
(8006+/8080+/5000+/9222+), so a VM cannot steal them while gitlab-2/-3 is down.

Bring gitlab-2 up once (a cold boot + reconfigure takes ~5-10 min):

    docker run -d --name gitlab-2 -p 8623:8023 gitlab-populated-final-port8023 \\
        /opt/gitlab/embedded/bin/runsvdir-start
    GITLAB_CONTAINER=gitlab-2 bash setup_gitlab_portless_url.sh   # external_url + listen port
    # then mint a root PAT (scopes api, sudo) on gitlab-2 -> GITLAB_ADMIN_TOKEN_2 in .env
    # gitlab-3 is the same on -p 8624:8023 -> GITLAB_ADMIN_TOKEN_3. Append the puma/sidekiq caps
    # (see gitlab-2's /etc/gitlab/gitlab.rb) BEFORE the reconfigure, or every page 500s.

Every per-episode reset rebuilds its own state (prepare_value_scenario, the throwaway
projects, the attacker issue), so a fresh container from the image needs no hand seeding.

Usage:
    from gitlab_instance import acquire_instance, get_current_instance

    inst = acquire_instance()   # call once; subsequent calls return the same inst
    # inst = {"id": 2, "port": 8623, "container": "gitlab-2", "token_env": "GITLAB_ADMIN_TOKEN_2"}

acquire_instance() exports GITLAB_PORT and GITLAB_CONTAINER, and copies the instance's own
admin token into GITLAB_ADMIN_TOKEN, so the injection / setup_cleanup / getter code (which all
read those three) targets the claimed instance without knowing about the pool. Pin a slot with
GITLAB_INSTANCE_ID; the pin still goes through the flock, so a busy slot raises.
"""
import fcntl
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_LOCK_DIR = "/tmp"

_INSTANCES = [
    {"id": 1, "port": 8023, "container": "gitlab",   "token_env": "GITLAB_ADMIN_TOKEN"},
    {"id": 2, "port": 8623, "container": "gitlab-2", "token_env": "GITLAB_ADMIN_TOKEN_2"},
    {"id": 3, "port": 8624, "container": "gitlab-3", "token_env": "GITLAB_ADMIN_TOKEN_3"},
]

_held_fd = None
_current_instance = None


def acquire_instance() -> dict:
    """Claim the first available GitLab instance via an exclusive flock.

    Idempotent: if this process already holds an instance, returns it immediately.
    Honours GITLAB_INSTANCE_ID to pin a specific slot. An instance whose admin token is not
    set is skipped (or, if pinned, raises): with the wrong instance's token every REST call
    401s and the getters refuse to score, so failing here is the loud, early version of that.

    Raises RuntimeError if no instance (or the pinned one) is available.
    """
    global _held_fd, _current_instance

    if _current_instance is not None:
        return _current_instance

    _pin = os.environ.get("GITLAB_INSTANCE_ID")
    _candidates = _INSTANCES
    if _pin:
        _candidates = [i for i in _INSTANCES if str(i["id"]) == _pin.strip()]
        if not _candidates:
            raise RuntimeError(
                f"GITLAB_INSTANCE_ID={_pin!r} names no instance; valid ids are "
                + ", ".join(str(i["id"]) for i in _INSTANCES))

    for inst in _candidates:
        token = os.environ.get(inst["token_env"], "")
        if not token:
            if _pin:
                raise RuntimeError(
                    f"GitLab instance {inst['id']} is pinned but {inst['token_env']} is not set.")
            logger.debug("GitLab instance %d has no %s, trying next.", inst["id"], inst["token_env"])
            continue
        lock_path = os.path.join(_LOCK_DIR, f"gitlab_instance_{inst['id']}.lock")
        try:
            fd = open(lock_path, "w")
        except PermissionError:
            logger.debug("GitLab instance %d unavailable (cannot open lock file), trying next.", inst["id"])
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            fd.close()
            logger.debug("GitLab instance %d is locked, trying next.", inst["id"])
            continue

        fd.write(str(os.getpid()))
        fd.flush()
        _held_fd = fd
        _current_instance = inst
        os.environ["GITLAB_PORT"] = str(inst["port"])
        os.environ["GITLAB_CONTAINER"] = inst["container"]
        os.environ["GITLAB_ADMIN_TOKEN"] = token
        logger.info(
            "Acquired GitLab instance %d (port %d, container '%s').",
            inst["id"], inst["port"], inst["container"],
        )
        return inst

    raise RuntimeError(
        "No free GitLab instance available — all are locked by other processes (or lack an "
        "admin token). Wait for a running evaluation to finish, or start fewer parallel runs."
    )


def get_current_instance() -> Optional[dict]:
    """Return the instance claimed by this process, or None if not yet acquired."""
    return _current_instance


def current_port() -> int:
    """The claimed instance's host port (8023 before any claim, i.e. the legacy single instance)."""
    return int(os.environ.get("GITLAB_PORT") or 8023)
