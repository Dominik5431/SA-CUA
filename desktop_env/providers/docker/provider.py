import logging
import os
import platform
import time
import docker
import psutil
import requests
from filelock import FileLock
from pathlib import Path

from desktop_env.providers.base import Provider

logger = logging.getLogger("desktopenv.providers.docker.DockerProvider")
logger.setLevel(logging.INFO)

WAIT_TIME = 3
RETRY_INTERVAL = 1
LOCK_TIMEOUT = 300


RESERVED_PORTS = (set(range(8023, 8031)) | set(range(8092, 8100)) | set(range(8110, 8131))
                  | set(range(8623, 8631)) | set(range(9990, 10000)))


def _parse_port_list(spec: str) -> set:
    ports = set()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        lo, _, hi = part.partition("-")
        ports.update(range(int(lo), int(hi or lo) + 1))
    return ports


class PortAllocationError(Exception):
    pass


class DockerProvider(Provider):
    def __init__(self, region: str):
        self.client = docker.from_env()
        self.server_port = None
        self.vnc_port = None
        self.chromium_port = None
        self.vlc_port = None
        self.container = None
        self.environment = {"DISK_SIZE": "32G", "RAM_SIZE": "4G", "CPU_CORES": "4"}

        temp_dir = Path(os.getenv('TEMP') if platform.system() == 'Windows' else '/tmp')
        self.lock_file = temp_dir / "docker_port_allocation.lck"
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)

    def _get_used_ports(self):
        """Get all currently used ports (both system and Docker)."""
        system_ports = set(conn.laddr.port for conn in psutil.net_connections())

        docker_ports = set()
        for container in self.client.containers.list(all=True):
            for mappings in (container.attrs['HostConfig'].get('PortBindings') or {}).values():
                docker_ports.update(int(p['HostPort']) for p in mappings or [] if p.get('HostPort'))
            ports = container.attrs['NetworkSettings']['Ports']
            if ports:
                for port_mappings in ports.values():
                    if port_mappings:
                        docker_ports.update(int(p['HostPort']) for p in port_mappings)

        reserved = RESERVED_PORTS | _parse_port_list(os.getenv("RTC_RESERVED_PORTS", ""))
        return system_ports | docker_ports | reserved

    def _get_available_port(self, start_port: int) -> int:
        """Find next available port starting from start_port."""
        used_ports = self._get_used_ports()
        port = start_port
        while port < 65354:
            if port not in used_ports:
                return port
            port += 1
        raise PortAllocationError(f"No available ports found starting from {start_port}")

    def _wait_for_vm_ready(self, timeout: int = 300):
        """Wait for VM to be ready by checking screenshot endpoint."""
        start_time = time.time()

        def check_screenshot():
            try:
                response = requests.get(
                    f"http://localhost:{self.server_port}/screenshot",
                    timeout=(10, 10)
                )
                return response.status_code == 200, f"HTTP {response.status_code}"
            except Exception as e:
                return False, f"{type(e).__name__}: {e}"

        while time.time() - start_time < timeout:
            ok, reason = check_screenshot()
            if ok:
                return True

            elapsed = time.time() - start_time

            if int(elapsed) % 10 == 0:
                try:
                    self.container.reload()
                    status = self.container.status
                    exit_code = self.container.attrs["State"].get("ExitCode")
                    error = self.container.attrs["State"].get("Error", "")
                    oom = self.container.attrs["State"].get("OOMKilled", False)

                    if status != "running":
                        recent_logs = self.container.logs(tail=20).decode(errors="replace")
                        logger.error(
                            f"Container is {status} (exit_code={exit_code}, oom={oom}, error='{error}'). "
                            f"Last logs:\n{recent_logs}"
                        )
                        raise RuntimeError(
                            f"Container exited unexpectedly: status={status}, "
                            f"exit_code={exit_code}, error='{error}'"
                        )

                    if int(elapsed) == 0 or int(elapsed) % 30 == 0:
                        binds = self.container.attrs["HostConfig"].get("Binds", [])
                        logger.info(
                            f"[t={elapsed:.0f}s] Container running | port={self.server_port} | "
                            f"probe='{reason}' | mounts={binds}"
                        )
                    else:
                        logger.info(
                            f"[t={elapsed:.0f}s] Checking if virtual machine is ready... "
                            f"(probe='{reason}')"
                        )
                except RuntimeError:
                    raise
                except Exception as e:
                    logger.warning(f"Could not inspect container: {e}")
            else:
                logger.info(
                    f"[t={elapsed:.0f}s] Checking if virtual machine is ready... "
                    f"(probe='{reason}')"
                )

            time.sleep(RETRY_INTERVAL)

        raise TimeoutError("VM failed to become ready within timeout period")

    DEFAULT_PORT_RANGES = [(8006, 8106), (5000, 5100), (9222, 9322), (8080, 8180)]

    def _cleanup_orphaned_containers(self, port_ranges: list = None):
        """Stop and remove containers whose creating process is no longer running.

        Only containers whose host ports overlap with port_ranges are cleaned up,
        leaving concurrent users on different port windows unaffected.
        """
        if port_ranges is None:
            port_ranges = self.DEFAULT_PORT_RANGES
        removed_any = False
        for container in self.client.containers.list(filters={"label": "desktop-env-pid"}):
            pid = int(container.labels["desktop-env-pid"])
            if not psutil.pid_exists(pid):
                container_ports = set()
                ports = container.attrs['NetworkSettings']['Ports']
                if ports:
                    for mappings in ports.values():
                        if mappings:
                            container_ports.update(int(p['HostPort']) for p in mappings)
                if not any(
                    start <= p <= end
                    for p in container_ports
                    for start, end in port_ranges
                ):
                    continue
                logger.info(f"Removing orphaned container {container.id[:12]} (PID {pid} is dead)")
                try:
                    container.stop()
                    container.remove()
                    removed_any = True
                except Exception as e:
                    logger.warning(f"Failed to remove orphaned container {container.id[:12]}: {e}")
        if removed_any:
            time.sleep(WAIT_TIME)

    def _ensure_ip_tables_loaded(self):
        with open("/proc/modules") as f:
            loaded = f.read()
        if "ip_tables" not in loaded:
            logger.info("ip_tables module not loaded, loading via privileged container...")
            self.client.containers.run(
                "alpine",
                command="modprobe ip_tables iptable_nat",
                remove=True,
                privileged=True,
                volumes={"/lib/modules": {"bind": "/lib/modules", "mode": "ro"}},
            )
            logger.info("ip_tables module loaded.")

    def start_emulator(self, path_to_vm: str, headless: bool, os_type: str):
        self._ensure_ip_tables_loaded()
        lock = FileLock(str(self.lock_file), timeout=LOCK_TIMEOUT)

        try:
            with lock:
                self._cleanup_orphaned_containers()
                self.vnc_port = self._get_available_port(8006)
                self.server_port = self._get_available_port(5000)
                self.chromium_port = self._get_available_port(9222)
                self.vlc_port = self._get_available_port(8080)

                self.container = self.client.containers.run(
                    "happysixd/osworld-docker",
                    environment=self.environment,
                    cap_add=["NET_ADMIN"],
                    devices=["/dev/kvm"],
                    volumes={
                        os.path.abspath(path_to_vm): {
                            "bind": "/System.qcow2",
                            "mode": "ro"
                        }
                    },
                    ports={
                        8006: self.vnc_port,
                        5000: self.server_port,
                        9222: self.chromium_port,
                        8080: self.vlc_port
                    },
                    labels={"desktop-env-pid": str(os.getpid())},
                    detach=True
                )

            logger.info(f"Started container with ports - VNC: {self.vnc_port}, "
                       f"Server: {self.server_port}, Chrome: {self.chromium_port}, VLC: {self.vlc_port}")

            self._wait_for_vm_ready()

        except Exception as e:
            if self.container:
                try:
                    self.container.stop()
                    self.container.remove()
                except:
                    pass
            raise e

    def get_ip_address(self, path_to_vm: str) -> str:
        if not all([self.server_port, self.chromium_port, self.vnc_port, self.vlc_port]):
            raise RuntimeError("VM not started - ports not allocated")
        return f"localhost:{self.server_port}:{self.chromium_port}:{self.vnc_port}:{self.vlc_port}"

    def save_state(self, path_to_vm: str, snapshot_name: str):
        raise NotImplementedError("Snapshots not available for Docker provider")

    def revert_to_snapshot(self, path_to_vm: str, snapshot_name: str):
        self.stop_emulator(path_to_vm)

    def stop_emulator(self, path_to_vm: str):
        if self.container:
            logger.info("Stopping VM...")
            try:
                self.container.stop()
                self.container.remove()
                time.sleep(WAIT_TIME)
            except Exception as e:
                logger.error(f"Error stopping container: {e}")
            finally:
                self.container = None
                self.server_port = None
                self.vnc_port = None
                self.chromium_port = None
                self.vlc_port = None
