import logging
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import psutil
import requests
from filelock import FileLock

from desktop_env.providers.base import Provider

logger = logging.getLogger("desktopenv.providers.singularity.SingularityProvider")
logger.setLevel(logging.INFO)

WAIT_TIME = 3
RETRY_INTERVAL = 1
LOCK_TIMEOUT = 10
DEFAULT_SIF_PATH = "./osworld-docker.sif"
DEFAULT_SERVER_PORT = int(os.getenv("OSWORLD_SINGULARITY_SERVER_PORT", "5000"))
DEFAULT_CHROMIUM_PORT = int(os.getenv("OSWORLD_SINGULARITY_CHROMIUM_PORT", "9222"))
DEFAULT_VLC_PORT = int(os.getenv("OSWORLD_SINGULARITY_VLC_PORT", "8080"))
DEFAULT_VNC_PORT = int(os.getenv("OSWORLD_SINGULARITY_VNC_PORT", "5900"))
DEFAULT_STORAGE_BASE_DIR = "/tmp/osworld-singularity-storage"
DEFAULT_VM_NET_IP = "20.20.20.21"
DEFAULT_VM_NET_GATEWAY = "20.20.20.1"
NETWORK_STUB_SCRIPT = Path(__file__).with_name("network_rootless.sh")


class PortAllocationError(Exception):
    pass


class SingularityProvider(Provider):
    """Headless-only Singularity provider for OSHarm."""

    def __init__(self, region: str):
        self.runtime_binary = self._get_runtime_binary()
        self.server_port = None
        self.chromium_port = None
        self.vlc_port = None
        self.vnc_port = None

        self.process = None
        self.log_handle = None
        self.log_path = None
        self.storage_dir = None
        self.nginx_stub_path = None

        self.environment = {
            "DISK_SIZE": "32G",
            "RAM_SIZE": "4G",
            "CPU_CORES": "4",
            "NETWORK": "slirp",
            "MONITOR": "none",
            "WEB_PORT": "8006",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

        temp_dir = Path(os.getenv("TEMP") if platform.system() == "Windows" else "/tmp")
        self.lock_file = temp_dir / "singularity_port_allocation.lck"
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _get_runtime_binary() -> str:
        for binary in ("singularity", "apptainer"):
            if shutil.which(binary):
                return binary
        raise FileNotFoundError("Neither 'singularity' nor 'apptainer' was found in PATH")

    def _get_used_ports(self):
        used_ports = set()
        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.laddr:
                    used_ports.add(conn.laddr.port)
        except Exception as e:
            logger.warning(f"Failed to enumerate used ports with psutil: {e}")
        return used_ports

    def _get_available_port(self, start_port: int) -> int:
        used_ports = self._get_used_ports()
        port = start_port
        while port < 65354:
            if port not in used_ports:
                return port
            port += 1
        raise PortAllocationError(f"No available ports found starting from {start_port}")

    def _assign_ports(self):
        self.server_port = self._get_available_port(DEFAULT_SERVER_PORT)
        self.chromium_port = self._get_available_port(DEFAULT_CHROMIUM_PORT)
        self.vlc_port = self._get_available_port(DEFAULT_VLC_PORT)
        self.vnc_port = self._get_available_port(DEFAULT_VNC_PORT)

    def _wait_for_vm_ready(self, timeout: int = 300):
        start_time = time.time()

        def check_screenshot():
            try:
                response = requests.get(
                    f"http://localhost:{self.server_port}/screenshot", timeout=(10, 10)
                )
                return response.status_code == 200
            except Exception:
                return False

        while time.time() - start_time < timeout:
            if self.process and self.process.poll() is not None:
                raise RuntimeError(
                    f"Singularity process exited early with code {self.process.returncode}. "
                    f"Check logs at: {self.log_path}"
                )

            if check_screenshot():
                return True
            logger.info("Checking if virtual machine is ready...")
            time.sleep(RETRY_INTERVAL)

        raise TimeoutError("VM failed to become ready within timeout period")

    @staticmethod
    def _resolve_vm_net_ip() -> str:
        return os.getenv("OSWORLD_SINGULARITY_VM_NET_IP", DEFAULT_VM_NET_IP)

    @staticmethod
    def _resolve_vm_net_gateway() -> str:
        return os.getenv("OSWORLD_SINGULARITY_VM_NET_GATEWAY", DEFAULT_VM_NET_GATEWAY)

    @staticmethod
    def _resolve_sif_path() -> str:
        sif_path = os.getenv("OSWORLD_SINGULARITY_SIF_PATH", DEFAULT_SIF_PATH)
        return os.path.abspath(os.path.expanduser(sif_path))

    @staticmethod
    def _resolve_storage_dir(path_to_vm: str) -> str:
        configured = os.getenv("OSWORLD_SINGULARITY_STORAGE_DIR", "").strip()
        if configured:
            storage_dir = os.path.abspath(os.path.expanduser(configured))
        else:
            vm_stem = Path(path_to_vm).stem
            storage_dir = os.path.abspath(os.path.join(DEFAULT_STORAGE_BASE_DIR, vm_stem))
        os.makedirs(storage_dir, exist_ok=True)
        return storage_dir

    @staticmethod
    def _prepare_nginx_stub() -> str:
        fd, stub_path = tempfile.mkstemp(prefix="osworld_nginx_stub_", suffix=".sh")
        os.close(fd)
        with open(stub_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
            f.write("echo '[osworld-singularity] nginx stub active' >&2\n")
            f.write("exit 0\n")
        os.chmod(stub_path, 0o755)
        return stub_path

    @staticmethod
    def _resolve_network_stub_path() -> str:
        if not NETWORK_STUB_SCRIPT.exists():
            raise FileNotFoundError(f"Network stub script not found: {NETWORK_STUB_SCRIPT}")
        return str(NETWORK_STUB_SCRIPT)

    def _inject_hosts_entry(self):
        import requests
        gateway = self._resolve_vm_net_gateway()
        cmd = f"echo password | sudo -S sh -c 'echo {gateway}  the-agent-company.com >> /etc/hosts'"
        try:
            resp = requests.post(
                f"http://localhost:{self.server_port}/execute",
                json={"command": ["bash", "-c", cmd]},
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("Injected the-agent-company.com into guest /etc/hosts")
        except Exception as e:
            logger.warning(f"Failed to inject hosts entry: {e}")

    def _build_command(
        self,
        path_to_vm: str,
        sif_path: str,
        storage_dir: str,
        nginx_stub_path: str,
        network_stub_path: str,
    ):
        vm_path = os.path.abspath(path_to_vm)
        if not os.path.exists(vm_path):
            raise FileNotFoundError(f"VM image not found: {vm_path}")
        if not os.path.exists(sif_path):
            raise FileNotFoundError(
                f"Singularity image not found: {sif_path}. "
                f"Set OSWORLD_SINGULARITY_SIF_PATH to your .sif file path."
            )

        command = [
            self.runtime_binary,
            "run",
            "--fakeroot",
            "--writable-tmpfs",
            "--bind",
            f"{vm_path}:/System.qcow2:ro",
            "--bind",
            f"{storage_dir}:/storage",
            "--bind",
            f"{nginx_stub_path}:/usr/sbin/nginx:ro",
            "--bind",
            f"{network_stub_path}:/run/network.sh:ro",
        ]

        for key, value in self.environment.items():
            command.extend(["--env", f"{key}={value}"])

        vm_net_ip = self._resolve_vm_net_ip()
        hostfwd_opts = (
            f"hostfwd=tcp:127.0.0.1:{self.server_port}-{vm_net_ip}:5000;"
            f"hostfwd=tcp:127.0.0.1:{self.chromium_port}-{vm_net_ip}:9222;"
            f"hostfwd=tcp:127.0.0.1:{self.vlc_port}-{vm_net_ip}:8080"
        )
        command.extend(["--env", f"HOSTFWD_OPTS={hostfwd_opts}"])
        command.extend(["--env", f"VM_NET_IP={vm_net_ip}"])
        command.extend(["--env", f"DISPLAY=vnc=:{self.vnc_port - DEFAULT_VNC_PORT}"])

        command.append(sif_path)
        return command

    def _cleanup_process(self):
        if self.process:
            try:
                if self.process.poll() is None:
                    if platform.system() == "Windows":
                        self.process.terminate()
                    else:
                        os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)

                    try:
                        self.process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        if platform.system() == "Windows":
                            self.process.kill()
                        else:
                            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                        self.process.wait(timeout=5)
            except Exception as e:
                logger.error(f"Error stopping singularity process: {e}")
            finally:
                self.process = None

        if self.log_handle:
            try:
                self.log_handle.close()
            except Exception:
                pass
            self.log_handle = None

        if self.nginx_stub_path:
            try:
                if os.path.exists(self.nginx_stub_path):
                    os.remove(self.nginx_stub_path)
            except Exception:
                pass
            self.nginx_stub_path = None


    def start_emulator(self, path_to_vm: str, headless: bool, os_type: str):
        if not headless:
            raise NotImplementedError(
                "SingularityProvider only supports headless=True (human viewer/noVNC is disabled)."
            )

        lock = FileLock(str(self.lock_file), timeout=LOCK_TIMEOUT)

        try:
            with lock:
                self._assign_ports()

                sif_path = self._resolve_sif_path()
                self.storage_dir = self._resolve_storage_dir(path_to_vm)
                self.nginx_stub_path = self._prepare_nginx_stub()
                network_stub_path = self._resolve_network_stub_path()
                command = self._build_command(
                    path_to_vm,
                    sif_path,
                    self.storage_dir,
                    self.nginx_stub_path,
                    network_stub_path,
                )

                timestamp = int(time.time())
                self.log_path = str(self.lock_file.parent / f"singularity_provider_{timestamp}.log")
                self.log_handle = open(self.log_path, "ab")

                self.process = subprocess.Popen(
                    command,
                    stdout=self.log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )

            logger.info(
                f"Started {self.runtime_binary} process "
                f"(fakeroot, writable-tmpfs) "
                f"with ports - Server: {self.server_port}, "
                f"Chrome: {self.chromium_port}, VLC: {self.vlc_port}, "
                f"VNC: {self.vnc_port}, "
                f"Storage: {self.storage_dir}"
            )

            self._wait_for_vm_ready()
            self._inject_hosts_entry()

        except Exception as e:
            self._cleanup_process()
            self.server_port = None
            self.chromium_port = None
            self.vlc_port = None
            self.vnc_port = None
            self.storage_dir = None
            self.nginx_stub_path = None
            raise e

    def get_ip_address(self, path_to_vm: str) -> str:
        if not all([self.server_port, self.chromium_port, self.vlc_port]):
            raise RuntimeError("VM not started - ports not allocated")
        return f"127.0.0.1:{self.server_port}:{self.chromium_port}:{self.vlc_port}"

    def save_state(self, path_to_vm: str, snapshot_name: str):
        raise NotImplementedError("Snapshots not available for Singularity provider")

    def revert_to_snapshot(self, path_to_vm: str, snapshot_name: str):
        self.stop_emulator(path_to_vm)

    def stop_emulator(self, path_to_vm: str):
        if self.process:
            logger.info("Stopping VM...")
            self._cleanup_process()
            time.sleep(WAIT_TIME)

        self.server_port = None
        self.chromium_port = None
        self.vlc_port = None
        self.vnc_port = None
        self.storage_dir = None
        self.nginx_stub_path = None
