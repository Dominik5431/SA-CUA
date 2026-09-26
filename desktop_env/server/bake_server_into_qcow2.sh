#!/usr/bin/env bash
# Bake the updated a11y server (main.py + window_stack.py) into the docker provider's BASE qcow2, so
# the window-stacking (st:stackorder / EWMH _NET_CLIENT_LIST_STACKING) fix survives a container
# revert. Needed because the docker provider boots each run from this pristine image (save_state is
# NotImplementedError; revert = remove+recreate the container), so a live edit to a running container
# is wiped on the next run.
#
# MUST run as root (libguestfs / image access). Run from the repo root:
#     sudo bash desktop_env/server/bake_server_into_qcow2.sh
#
# Safe by construction: refuses to touch the image while a container has it open, verifies the files
# land, and leaves a timestamped backup of the two replaced guest files next to the image.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
QCOW2="${QCOW2:-$REPO_ROOT/docker_vm_data/Ubuntu.qcow2}"
GUEST_DIR="/home/user/server"
SRC_DIR="$REPO_ROOT/desktop_env/server"
MARKER_MAIN="window_stacking_order"
MARKER_WS="_NET_CLIENT_LIST_STACKING"

log(){ echo "[bake] $*"; }
die(){ echo "[bake] ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root (needs libguestfs/image access): sudo bash $0"
[ -f "$QCOW2" ] || die "qcow2 not found: $QCOW2 (set QCOW2=/path/to/Ubuntu.qcow2)"
[ -f "$SRC_DIR/main.py" ] || die "missing $SRC_DIR/main.py"
[ -f "$SRC_DIR/window_stack.py" ] || die "missing $SRC_DIR/window_stack.py"
grep -q "$MARKER_MAIN" "$SRC_DIR/main.py" || die "$SRC_DIR/main.py lacks $MARKER_MAIN -- not the updated server?"
grep -q "$MARKER_WS" "$SRC_DIR/window_stack.py" || die "$SRC_DIR/window_stack.py lacks $MARKER_WS"

command -v virt-copy-in >/dev/null 2>&1 || {
    log "libguestfs-tools not found; installing (apt-get)..."
    apt-get update -y && apt-get install -y libguestfs-tools || die "could not install libguestfs-tools"
}

# Refuse to modify an image a container still has open (docker mounts it as /System.qcow2).
QCOW2_ABS="$(readlink -f "$QCOW2")"
if command -v docker >/dev/null 2>&1; then
    for cid in $(docker ps --format '{{.ID}}'); do
        # one mount source per line, so a substring/exact match is reliable
        if docker inspect "$cid" --format '{{range .Mounts}}{{println .Source}}{{end}}' 2>/dev/null \
             | grep -qxF "$QCOW2_ABS"; then
            die "container $cid has $QCOW2 open -- stop it first (docker stop $cid) so the write is safe"
        fi
    done
fi
# Extra safety: fail if anything holds the file open.
if command -v fuser >/dev/null 2>&1 && fuser "$QCOW2" >/dev/null 2>&1; then
    die "$QCOW2 is open by another process -- close it before baking"
fi

STAMP="$(date +%Y%m%d%H%M%S)"
BACKUP="$QCOW2.serverfiles.bak.$STAMP"
log "backing up the two guest files currently in the image -> $BACKUP.tar"
mkdir -p "/tmp/bake.$STAMP"
virt-copy-out -a "$QCOW2" "$GUEST_DIR/main.py" "/tmp/bake.$STAMP/" 2>/dev/null || log "(no existing main.py to back up)"
virt-copy-out -a "$QCOW2" "$GUEST_DIR/window_stack.py" "/tmp/bake.$STAMP/" 2>/dev/null || log "(no existing window_stack.py)"
tar -C "/tmp/bake.$STAMP" -cf "$BACKUP.tar" . 2>/dev/null || true
rm -rf "/tmp/bake.$STAMP"

log "copying updated server into the image at $GUEST_DIR/ ..."
virt-copy-in -a "$QCOW2" "$SRC_DIR/main.py" "$SRC_DIR/window_stack.py" "$GUEST_DIR/"

log "verifying the image now carries the new code..."
virt-cat -a "$QCOW2" "$GUEST_DIR/main.py"        | grep -q "$MARKER_MAIN" || die "verify FAILED: main.py marker absent in image"
virt-cat -a "$QCOW2" "$GUEST_DIR/window_stack.py" | grep -q "$MARKER_WS"  || die "verify FAILED: window_stack.py marker absent in image"

log "done. $QCOW2 now boots the updated server; the next run emits st:stackorder."
log "note: if you also use at-spi/docker_vm_data/Ubuntu.qcow2, re-run with QCOW2=that path."
