#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# bake_a11y_fix_into_qcow2.sh — install the patched a11y server into the VM image
#
# Fixes the fault that took /accessibility down for the rest of an episode: a Chrome
# navigation drops hyperlinks between _hypertext.getNLinks() and getLink(i), getLink()
# returns None, and `_hl.getURI(0)` raised AttributeError -- which the surrounding
# `except NotImplementedError` does not catch.  The exception escaped the worker thread,
# future.result() re-raised it in the /accessibility handler, and the endpoint 500'd for the
# WHOLE desktop until the page settled: healthy at baseline and with Chrome open, broken
# on the very next poll after a navigation.
#
# Two changes, both in desktop_env/server/main.py:
#   1. skip a stale link index instead of dereferencing None (the root cause)
#   2. guard each application's future in /accessibility, so one app's subtree can never
#      cost the caller every other app's -- a partial tree beats no tree
#
# Same mechanics as bake_docker_into_qcow2.sh: libguestfs inside a privileged container, so
# no host sudo.  Needs a kernel for the supermin appliance plus /dev/kvm.
#
# Writes to a SEPARATE image by default and leaves the live one alone, so this can be built
# and verified while evaluations are still running.  Swap it in deliberately:
#
#     mv docker_vm_data/Ubuntu.qcow2        docker_vm_data/Ubuntu.pre-a11yfix.qcow2
#     mv docker_vm_data/Ubuntu.a11yfix.qcow2 docker_vm_data/Ubuntu.qcow2
#
# Usage:  bash bake_a11y_fix_into_qcow2.sh [target.qcow2] [source_main.py]
#   - target qcow2 must be FREE (no container may have it bound)
# ---------------------------------------------------------------------------
set -euo pipefail

TARGET="${1:-docker_vm_data/Ubuntu.a11yfix.qcow2}"
SERVER_SRC="${2:-desktop_env/server/main.py}"
GUEST_PATH="/home/user/server/main.py"

[[ -f "$TARGET" ]]     || { echo "ERROR: target qcow2 not found: $TARGET"; exit 1; }
[[ -f "$SERVER_SRC" ]] || { echo "ERROR: server source not found: $SERVER_SRC"; exit 1; }

python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$SERVER_SRC" \
  || { echo "ERROR: $SERVER_SRC does not parse; refusing to bake it"; exit 1; }

# Refuse to touch an image a container currently has bound -- the guest would keep serving
# the old code and the result would be silently confusing.
if docker ps --format '{{.Names}}' | grep -q . ; then
  ABS_TARGET=$(readlink -f "$TARGET")
  for c in $(docker ps --format '{{.Names}}'); do
    if docker inspect "$c" --format '{{range .Mounts}}{{.Source}} {{end}}' 2>/dev/null \
        | tr ' ' '\n' | grep -qx "$ABS_TARGET"; then
      echo "ERROR: container '$c' has $TARGET bound; stop it first."; exit 1
    fi
  done
fi

DIR=$(cd "$(dirname "$TARGET")" && pwd)
BASE=$(basename "$TARGET")
STAGE="$DIR/.main.py.staged"
cp "$SERVER_SRC" "$STAGE"
trap 'rm -f "$STAGE"' EXIT

echo "baking $SERVER_SRC -> $GUEST_PATH inside $TARGET ..."
docker run --rm --privileged --device /dev/kvm -v "$DIR:/data" ubuntu:22.04 bash -c '
    set -e
    export DEBIAN_FRONTEND=noninteractive LIBGUESTFS_BACKEND=direct
    apt-get update -qq >/dev/null 2>&1
    apt-get install -y -qq libguestfs-tools linux-image-generic >/dev/null 2>&1
    virt-customize -a "/data/'"$BASE"'" \
      --upload "/data/.main.py.staged:'"$GUEST_PATH"'" \
      --run-command "chown user:user '"$GUEST_PATH"' 2>/dev/null || true"
'
echo "done.  $TARGET now serves the patched a11y endpoint."
echo "Verify by polling /accessibility on a VM booted on $BASE across a Chrome navigation."
