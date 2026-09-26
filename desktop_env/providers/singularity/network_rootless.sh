#!/usr/bin/env bash
set -Eeuo pipefail

# Rootless networking for qemu-docker via Singularity/Apptainer.
# HOSTFWD_OPTS is set by the Singularity provider with the full hostfwd string.

: "${ADAPTER:=virtio-net-pci}"
: "${VM_NET_HOST:=$APP}"
: "${VM_NET_IP:=20.20.20.21}"

if [ -z "${MAC:-}" ]; then
  MAC=$(echo "$HOST" | md5sum | sed "s/^\(..\)\(..\)\(..\)\(..\)\(..\).*$/02:\1:\2:\3:\4:\5/")
fi
VM_NET_MAC="${MAC^^}"
VM_NET_MAC="${VM_NET_MAC//-/:}"

gateway="${VM_NET_IP%.*}.1"
NET_OPTS="-netdev user,id=hostnet0,host=$gateway,net=${VM_NET_IP%.*}.0/24,dhcpstart=$VM_NET_IP,hostname=$VM_NET_HOST"

# Replace semicolons with commas (commas break Apptainer --env parsing).
if [ -n "${HOSTFWD_OPTS:-}" ]; then
    HOSTFWD_OPTS="${HOSTFWD_OPTS//;/,}"
fi

if [ -n "${HOSTFWD_OPTS:-}" ]; then
  NET_OPTS+=",$HOSTFWD_OPTS"
fi

NET_OPTS+=" -device $ADAPTER,id=net0,netdev=hostnet0,romfile=,mac=$VM_NET_MAC"

closeNetwork() { return 0; }

html "Initialized network successfully..."
return 0
