#!/usr/bin/env python3
"""
deploy_server_update.py -- push an updated a11y server (main.py) INTO a running VM and, optionally,
bake it into the snapshot the task runner reverts to.

WHY THIS EXISTS. `desktop_env/server/main.py` runs INSIDE the guest as the `osworld_server`
systemd service; nothing copies the host repo's copy into the guest per run, and every task reverts
to the `init_state` snapshot (`desktop_env.py::_revert_to_snapshot`). So a host-side edit to the
server (e.g. the EWMH `_NET_CLIENT_LIST_STACKING` / `st:stackorder` change) is inert until the guest
is updated AND that update is captured in the reverted snapshot. This script does both, over the
server's own HTTP control routes -- no scp/ssh needed.

WHAT IT DOES
  1. health-check the running server                       (GET  /platform)
  2. find where main.py is installed in the guest          (POST /execute: systemctl cat)
  3. back up the current guest main.py
  4. upload the new main.py                                (POST /setup/upload)
  5. confirm the transfer parses and carries the new code  (POST /execute: python3 -c ..., grep)
  6. restart the service and wait for it to come back      (POST /execute; poll /platform)
  7. verify the live tree now carries st:stackorder        (GET  /accessibility)
  8. (opt) re-save the snapshot so the per-task revert keeps it   (--save-snapshot, vmrun)

USAGE
  # point straight at the guest:
  python desktop_env/server/deploy_server_update.py --vm-ip 192.168.x.y

  # let it resolve the IP from the .vmx AND re-snapshot in one go (vmware):
  python desktop_env/server/deploy_server_update.py \
      --path-to-vm ./vmware_vm_data/Ubuntu0/Ubuntu0.vmx --save-snapshot

Only the guest-facing steps (1-7) need the VM running. Step 8 is vmware-only here and is the one
destructive action -- it deletes and recreates the named snapshot, so it is opt-in.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_DEFAULT_SERVER_FILE = os.path.join(_HERE, "main.py")
_COMPANION_FILE = os.path.join(_HERE, "window_stack.py")
_MAIN_MARKER = "window_stacking_order"
_COMPANION_MARKER = "_NET_CLIENT_LIST_STACKING"
_NEW_CODE_MARKER = _MAIN_MARKER


def _log(msg: str) -> None:
    print(f"[deploy] {msg}", flush=True)


def _die(msg: str, code: int = 1):
    print(f"[deploy] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)



def _execute(base: str, command, shell: bool = False, timeout: int = 60) -> dict:
    """Run a command in the guest via /execute. Returns the parsed JSON (status/output/error/rc)."""
    r = requests.post(f"{base}/execute", json={"command": command, "shell": shell}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _execute_maybe_sudo(base: str, argv: list, timeout: int = 60) -> dict:
    """Try a command as-is; on a permission failure retry with passwordless sudo. The server may run
    as root (no sudo needed) or as a user with NOPASSWD sudo -- both are common in OSWorld images."""
    res = _execute(base, argv, timeout=timeout)
    if res.get("returncode") == 0:
        return res
    err = (res.get("error") or "") + (res.get("message") or "")
    if "permission" in err.lower() or "denied" in err.lower() or res.get("returncode") not in (0, None):
        res2 = _execute(base, ["sudo", "-n", *argv], timeout=timeout)
        if res2.get("returncode") == 0:
            return res2
        return res2 if len(str(res2.get("error") or "")) else res
    return res


def _health(base: str) -> bool:
    try:
        r = requests.get(f"{base}/platform", timeout=10)
        return r.status_code == 200 and "Linux" in r.text
    except requests.RequestException:
        return False


def _detect_guest_path(base: str, service: str, override: "str | None") -> str:
    if override:
        return override
    try:
        res = _execute(base, ["systemctl", "cat", service])
        text = (res.get("output") or "") + (res.get("error") or "")
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("ExecStart"):
                continue
            for tok in line.replace("=", " ").split():
                if tok.endswith("main.py"):
                    _log(f"detected guest server path from {service}: {tok}")
                    return tok
    except Exception as e:
        _log(f"could not read {service} (falling back to default guest path): {e}")
    fallback = "/root/main.py"
    _log(f"WARNING: assuming guest server path {fallback} -- pass --guest-path to override")
    return fallback


def _backup(base: str, guest_path: str) -> None:
    stamp = time.strftime("%Y%m%d%H%M%S")
    res = _execute_maybe_sudo(base, ["cp", guest_path, f"{guest_path}.bak.{stamp}"])
    if res.get("returncode") == 0:
        _log(f"backed up guest {guest_path} -> {guest_path}.bak.{stamp}")
    else:
        _log(f"WARNING: could not back up {guest_path}: {res.get('error') or res.get('message')}")


def _upload(base: str, local_file: str, guest_path: str) -> None:
    with open(local_file, "rb") as f:
        r = requests.post(f"{base}/setup/upload",
                          data={"file_path": guest_path},
                          files={"file_data": (os.path.basename(guest_path), f)},
                          timeout=60)
    if r.status_code != 200:
        _die(f"upload failed ({r.status_code}): {r.text[:200]}")
    _log(f"uploaded {os.path.relpath(local_file, _REPO_ROOT)} -> guest {guest_path}")


def _verify_uploaded(base: str, guest_path: str, marker: str) -> None:
    res = _execute(base, ["python3", "-c", f"import ast,sys; ast.parse(open('{guest_path}').read())"])
    if res.get("returncode") != 0:
        _die(f"{guest_path} does not parse in the guest: {res.get('error') or res.get('message')}")
    res = _execute(base, ["grep", "-c", marker, guest_path])
    if (res.get("output") or "0").strip() == "0":
        _die(f"{guest_path} is missing the new-code marker ({marker}); wrong file?")
    _log(f"verified {guest_path} parses and contains {marker!r}")


def _restart(base: str, service: str) -> None:
    for argv in ([ "systemctl", "--no-block", "restart", service],
                 ["sudo", "-n", "systemctl", "--no-block", "restart", service]):
        try:
            res = _execute(base, argv, timeout=15)
            if res.get("returncode") == 0:
                _log(f"restart queued: {' '.join(argv)}")
                return
        except requests.RequestException:
            _log("restart request dropped the connection (server is going down) -- expected")
            return
    _log("WARNING: restart command returned non-zero; will still poll for the server to come back")


def _wait_up(base: str, timeout: int = 90) -> None:
    _log(f"waiting for the server to come back (up to {timeout}s)...")
    deadline = time.time() + timeout
    time.sleep(2)
    while time.time() < deadline:
        if _health(base):
            _log("server is back up")
            return
        time.sleep(2)
    _die("server did not come back after restart -- check the VM and osworld_server.service logs")


def _verify_stackorder(base: str) -> int:
    """Count top-level windows the live tree now tags with st:stackorder. 0 is not fatal (a desktop
    with no managed windows), but a positive count is direct proof the new emitter is running."""
    try:
        r = requests.get(f"{base}/accessibility", timeout=30)
        at = r.json().get("AT", "")
    except Exception as e:
        _log(f"could not fetch /accessibility to verify: {e}")
        return -1
    n = at.count("stackorder")
    _log(f"live tree carries st:stackorder on {n} window(s)")
    return n



def _resolve_ip(path_to_vm: str) -> str:
    sys.path.insert(0, _REPO_ROOT)
    from desktop_env.providers.vmware.provider import VMwareProvider
    ip = VMwareProvider().get_ip_address(path_to_vm)
    _log(f"resolved guest IP from {os.path.basename(path_to_vm)}: {ip}")
    return ip


def _resnapshot(path_to_vm: str, name: str) -> None:
    sys.path.insert(0, _REPO_ROOT)
    import subprocess
    from desktop_env.providers.vmware.provider import VMwareProvider, get_vmrun_type
    _log(f"replacing snapshot {name!r} on {os.path.basename(path_to_vm)} (delete + recreate)...")
    subprocess.run(["vmrun"] + get_vmrun_type(return_list=True) + ["deleteSnapshot", path_to_vm, name],
                   capture_output=True, text=True)
    VMwareProvider().save_state(path_to_vm, name)
    _log(f"snapshot {name!r} now captures the updated server -- the per-task revert will keep it")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vm-ip", help="guest IP of the running VM (or use --path-to-vm to resolve it)")
    ap.add_argument("--path-to-vm", help="path to the .vmx (vmware) -- resolves --vm-ip and enables --save-snapshot")
    ap.add_argument("--port", type=int, default=5000, help="a11y server port (default 5000)")
    ap.add_argument("--server-file", default=_DEFAULT_SERVER_FILE,
                    help="local main.py to push (default: this repo's desktop_env/server/main.py)")
    ap.add_argument("--guest-path", help="installed server path in the guest (default: auto-detect from the service)")
    ap.add_argument("--service", default="osworld_server.service", help="systemd unit name")
    ap.add_argument("--save-snapshot", action="store_true",
                    help="after updating, delete+recreate the snapshot so the per-task revert keeps it (DESTRUCTIVE)")
    ap.add_argument("--snapshot-name", default="init_state", help="snapshot to replace (default init_state)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit without changing anything")
    args = ap.parse_args()

    if not os.path.isfile(args.server_file):
        _die(f"server file not found: {args.server_file}")
    if _MAIN_MARKER not in open(args.server_file, encoding="utf-8").read():
        _die(f"{args.server_file} does not contain {_MAIN_MARKER!r} -- is this the updated server?")
    if not os.path.isfile(_COMPANION_FILE):
        _die(f"companion file not found: {_COMPANION_FILE}")
    if _COMPANION_MARKER not in open(_COMPANION_FILE, encoding="utf-8").read():
        _die(f"{_COMPANION_FILE} does not contain {_COMPANION_MARKER!r}")

    vm_ip = args.vm_ip
    if not vm_ip:
        if not args.path_to_vm:
            _die("need --vm-ip or --path-to-vm")
        vm_ip = _resolve_ip(args.path_to_vm)
    base = f"http://{vm_ip}:{args.port}"

    if args.dry_run:
        _log(f"DRY RUN -- would update {base} from {args.server_file}"
             + (f", then re-snapshot {args.snapshot_name!r} on {args.path_to_vm}" if args.save_snapshot else ""))
        return 0

    _log(f"target server: {base}")
    if not _health(base):
        _die(f"server not reachable at {base}/platform -- is the VM running?")

    guest_path = _detect_guest_path(base, args.service, args.guest_path)
    guest_companion = os.path.join(os.path.dirname(guest_path), os.path.basename(_COMPANION_FILE))
    _backup(base, guest_path)
    _upload(base, _COMPANION_FILE, guest_companion)
    _verify_uploaded(base, guest_companion, _COMPANION_MARKER)
    _upload(base, args.server_file, guest_path)
    _verify_uploaded(base, guest_path, _MAIN_MARKER)
    _restart(base, args.service)
    _wait_up(base)
    _verify_uploaded(base, guest_path, _MAIN_MARKER)
    _verify_stackorder(base)

    if args.save_snapshot:
        if not args.path_to_vm:
            _die("--save-snapshot needs --path-to-vm")
        _resnapshot(args.path_to_vm, args.snapshot_name)
    else:
        _log("server updated in the RUNNING VM only. It will be lost on the next per-task revert -- "
             "re-run with --save-snapshot (needs --path-to-vm) to bake it into the reverted snapshot.")

    _log("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
