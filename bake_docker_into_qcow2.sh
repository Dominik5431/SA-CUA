#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# bake_docker_into_qcow2.sh — install Docker into the OSWorld VM image
#
# own_owncloud_3's benign goal is `docker pull alpine`, but the stock qcow2 ships no
# Docker.  Runtime install via a task setup step does not fit the guest's 120s /execute
# cap, so Docker is baked into the image here, once.
#
# Three things learned the hard way, all encoded below:
#   1. Modify the image with virt-customize (libguestfs), NOT a raw qemu-nbd + chroot +
#      lazy-umount -- that left the ext4 dirty and the VM hung at boot.
#   2. Docker must be DISABLED at boot.  dockerd's iptables/bridge setup at boot breaks the
#      harness<->VM network path, so the /execute server never becomes reachable and the VM
#      looks hung.  A per-episode setup step starts the daemon instead (add_docker_setup.py).
#   3. /etc/docker/daemon.json must set {"iptables": false, "bridge": "none"} so that even
#      when the daemon is started on-demand it does not rewrite iptables and cut the harness
#      connection.  `docker pull` still works (it needs only the VM's normal outbound net).
#
# Runs virt-customize inside a privileged container (Docker's daemon is already root), so no
# host sudo is needed.  The container needs a kernel (linux-image-generic) for libguestfs's
# supermin appliance, plus /dev/kvm.
#
# Usage:  bash bake_docker_into_qcow2.sh [path/to/Ubuntu.qcow2]
#   - qcow2 must be FREE (no osworld container running).
#   - a backup <qcow2>.pre-docker.bak is made if absent.
# ---------------------------------------------------------------------------
set -euo pipefail
QCOW="${1:-docker_vm_data/Ubuntu.qcow2}"
[[ -f "$QCOW" ]] || { echo "ERROR: qcow2 not found: $QCOW"; exit 1; }
DIR=$(cd "$(dirname "$QCOW")" && pwd)
BASE=$(basename "$QCOW")

[[ -f "$DIR/$BASE.pre-docker.bak" ]] || { echo "backing up -> $BASE.pre-docker.bak"; cp -n "$QCOW" "$DIR/$BASE.pre-docker.bak"; }

echo "baking Docker into $QCOW via virt-customize (container)..."
docker run --rm --privileged --device /dev/kvm -v "$DIR:/data" ubuntu:22.04 bash -c '
    set -e
    export DEBIAN_FRONTEND=noninteractive LIBGUESTFS_BACKEND=direct
    apt-get update -qq >/dev/null 2>&1
    apt-get install -y -qq libguestfs-tools linux-image-generic >/dev/null 2>&1
    virt-customize -a "/data/'"$BASE"'" \
      --install docker.io \
      --write "/etc/docker/daemon.json:{\"iptables\": false, \"bridge\": \"none\"}" \
      --run-command "systemctl disable docker.service docker.socket containerd.service; true"
'
echo "done.  Docker installed, disabled at boot, iptables:false.  Per-episode start via add_docker_setup.py."
