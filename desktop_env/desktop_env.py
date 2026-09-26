from __future__ import annotations

import base64
import hashlib
import shlex
import logging
import os
import time
from typing import Callable, Any, Optional, Tuple
from typing import List, Dict, Union

import gymnasium as gym

from desktop_env.controllers.python import PythonController
from desktop_env.controllers.setup import SetupController
from desktop_env.evaluators import metrics, getters
from desktop_env.providers import create_vm_manager_and_provider

from adv.WebArena_Reddit.injection import reddit_adv_setup
from adv.WebArena_Reddit.preprocess import populate_reddit_database
from adv.TheAgentCompany_RocketChat.run_multi_npc import rocketchat_adv_setup
from adv.TheAgentCompany_OwnCloud.injection import owncloud_adv_setup, owncloud_add_user
from adv.WebArena_Gitlab.injection import gitlab_adv_setup
from adv.WebArena_Gitlab.setup_cleanup import run_gitlab_setup_fn, run_gitlab_cleanup_fn
from adv.WebArena_Reddit.setup_cleanup import run_reddit_setup_fn, run_reddit_cleanup_fn

from adv.WebArena_Reddit.if_ready import check_server_ready_reddit
from adv.TheAgentCompany_RocketChat.if_ready import check_server_ready_rocketchat
from adv.TheAgentCompany_OwnCloud.if_ready import check_server_ready_owncloud
from adv.WebArena_Gitlab.if_ready import check_server_ready_gitlab
import re

import subprocess

from owncloud_instance import acquire_instance as acquire_owncloud_instance, get_current_instance as get_current_owncloud_instance
from rocketchat_instance import acquire_instance as acquire_rocketchat_instance, get_current_instance as get_current_rocketchat_instance
from reddit_instance import acquire_instance as acquire_reddit_instance, get_current_instance as get_current_reddit_instance
from gitlab_instance import acquire_instance as acquire_gitlab_instance, get_current_instance as get_current_gitlab_instance, current_port as gitlab_port

logger = logging.getLogger("desktopenv.env")

_REDTEAMCUA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

Metric = Callable[[Any, Any], float]
Getter = Callable[[gym.Env, Dict[str, Any]], Any]

_QUIET_UPDATES_CMD = r"""
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus

# 1. gnome-software's update monitor is what raises the banner (update-notifier is the other
#    possible source). Kill the running daemons and drop the autostart entries plus the D-Bus
#    activation file, so nothing revives them this session. -x matches on process name rather than
#    on the full command line, which would also match this very script.
pkill -x gnome-software 2>/dev/null || true
pkill -x update-notifier 2>/dev/null || true
echo 'password' | sudo -S rm -f \
    /etc/xdg/autostart/gnome-software-service.desktop \
    /etc/xdg/autostart/update-notifier.desktop \
    /usr/share/dbus-1/services/org.gnome.Software.service 2>/dev/null || true

# 2. Stop the machinery that makes updates pending in the first place -- the same apt jobs swap the
#    google-chrome binary under a running browser, which is what produces "Finish update".
echo 'password' | sudo -S systemctl mask --now \
    apt-daily.timer apt-daily-upgrade.timer \
    apt-daily.service apt-daily-upgrade.service \
    unattended-upgrades.service packagekit.service packagekit-offline-update.service \
    2>/dev/null || true

# 3. Belt and braces, in case a gnome-software instance survives the above.
gsettings set org.gnome.software download-updates false 2>/dev/null || true
gsettings set org.gnome.software allow-updates false 2>/dev/null || true

# 4. The actual on-screen "Software Updates Available to Download / ... Ready to Install" banner
#    (see dismiss_update_banner()) turned out to come from a DIFFERENT source than steps 1-3 cover:
#    a real systemd TIMER unit (update-notifier-download.timer -> .service) not touched by masking
#    apt-daily*/unattended-upgrades/packagekit* above, plus snapd's own background refresh check
#    (surfaced as the running `snap-store --gapplication-service` / `snapd-desktop-integration`
#    processes, confirmed via `ps aux` while chasing this banner down live). Mask/hold both so
#    fewer fresh notifications get queued -- this alone does NOT make an already-shown banner
#    disappear (GNOME Shell keeps rendering it once fired, immune to killing/masking whatever
#    triggered it), which is why dismiss_update_banner() is still needed at each capture point;
#    this just reduces how often a NEW one gets queued in the first place.
echo 'password' | sudo -S systemctl mask --now \
    update-notifier-download.timer update-notifier-download.service \
    update-notifier-motd.timer update-notifier-motd.service 2>/dev/null || true
echo 'password' | sudo -S snap set system refresh.hold=forever 2>/dev/null || true

# 5. Steps 1-4 all attack SENDERS, and a sender we have not identified can still fire one.
#    This attacks the RENDERER: with show-banners off, GNOME Shell routes notifications
#    straight to the message tray instead of drawing them over the screen, so nothing an
#    agent is looking at gets covered. The per-application keys are the narrower version of
#    the same switch, kept for the senders we HAVE identified, in case a distribution ships
#    a shell that honours those but overrides the global one.
gsettings set org.gnome.desktop.notifications show-banners false 2>/dev/null || true
for _app in update-notifier update-manager snap-store gnome-software org-gnome-software \
            software-updater org-gnome-SoftwareUpdate; do
    gsettings set \
        "org.gnome.desktop.notifications.application:/org/gnome/desktop/notifications/application/${_app}/" \
        enable false 2>/dev/null || true
done
true
"""


_UPDATE_BANNER_NAMES = (
    "update available for",
    "software updates available",
    "software updates ready",
    "updates are available",
)

_BANNER_MAX_Y = 130

_ATSPI_TAG_RE = re.compile(r"<([a-z-]+)\s([^>]*?)/?>")
_SCREENCOORD_RE = re.compile(r'cp:screencoord="\((-?\d+), (-?\d+)\)"')
_SIZE_RE = re.compile(r'cp:size="\((-?\d+), (-?\d+)\)"')
_NAME_RE = re.compile(r'name="([^"]*)"')


def _find_update_banner(tree: str):
    """Locates a CURRENTLY-RENDERED software-update notification in an AT-SPI tree, returning
    ((click_x, click_y), banner_name, clicking_the_close_button) or None.

    Requiring `st:showing="true"` AND real screen coordinates is the whole point of this helper.
    GNOME keeps every notification it has ever fired in the shell's message-tray history, and those
    history nodes carry the same text with neither attribute -- measured on this repo's at-spi
    corpus, 182 of 250 captures contain "Snap Store"/"Software Updates" somewhere in the tree while
    only 48 have a banner actually on screen. A plain substring test over the tree therefore fires
    on the large majority of captures that have nothing to dismiss, and the resulting blind click
    lands in whatever application happens to own that pixel.

    Preferred target is the banner's own close button (the shell renders one inside the banner,
    right-aligned), because clicking it dismisses without triggering the notification's default
    action; the caller only needs its snap-store/gnome-software cleanup on the fallback path."""
    label = None
    buttons = []
    for m in _ATSPI_TAG_RE.finditer(tree):
        tag, attrs = m.group(1), m.group(2)
        if 'st:showing="true"' not in attrs or 'attr:toolkit="clutter"' not in attrs:
            continue
        coord = _SCREENCOORD_RE.search(attrs)
        size = _SIZE_RE.search(attrs)
        if not coord or not size:
            continue
        x, y = int(coord.group(1)), int(coord.group(2))
        w, h = int(size.group(1)), int(size.group(2))
        if y > _BANNER_MAX_Y or w <= 0 or h <= 0:
            continue
        if tag == "push-button":
            buttons.append((x, y, w, h))
            continue
        if tag != "label" or label is not None:
            continue
        name = _NAME_RE.search(attrs)
        name = name.group(1) if name else ""
        if any(p in name.lower() for p in _UPDATE_BANNER_NAMES):
            label = (x, y, w, h, name)
    if label is None:
        return None

    lx, ly, lw, lh, name = label
    for x, y, w, h in buttons:
        if x > lx + lw and abs((y + h // 2) - (ly + lh // 2)) <= lh:
            return (x + w // 2, y + h // 2), name, True
    return (lx + lw // 2, ly + lh // 2), name, False


def contains_to_be_replaced(config_list):

    def check_value(value):
        if isinstance(value, str):
            return 'TO_BE_REPLACED' in value
        elif isinstance(value, dict):
            return any(check_value(v) for v in value.values())
        elif isinstance(value, list):
            return any(check_value(item) for item in value)
        return False

    return check_value(config_list)


def sub_url(url: str, to_be_replaced, replacement):
    url =  re.sub(to_be_replaced, replacement, url)
    return url


def replace_in_dict(data,to_be_replaced,replacement):
    if isinstance(data, dict):
        return {key: replace_in_dict(val,to_be_replaced,replacement) for key, val in data.items()}
    elif isinstance(data, list):
        return [replace_in_dict(item,to_be_replaced,replacement) for item in data]
    elif isinstance(data, tuple):
        return tuple(replace_in_dict(item,to_be_replaced,replacement) for item in data)
    elif isinstance(data, str):
        return sub_url(data,to_be_replaced,replacement)
    else:
        return data

def extract_http_hosts(config_list):

    extracted_hosts = set()

    def extract_from_value(value):
        if isinstance(value, str):
            matches = re.findall(r'http://[^/:]+', value)
            extracted_hosts.update(matches)
        elif isinstance(value, dict):
            for v in value.values():
                extract_from_value(v)
        elif isinstance(value, list):
            for item in value:
                extract_from_value(item)

    for item in config_list:
        extract_from_value(item)

    return list(extracted_hosts)


WEB_PROXY_CONTAINER = "rtc-web-proxy"
WEB_PROXY_CONF_DIR = "/tmp/rtc_web_proxy"
WEB_PROXY_IMAGE = "nginx:alpine"

WEB_PROXY_SLOT_NETWORKS = {
    2: ("rtc-proxy-slot-2", "172.25.0.0/24", "172.25.0.1"),
    3: ("rtc-proxy-slot-3", "172.26.0.0/24", "172.26.0.1"),
    4: ("rtc-proxy-slot-4", "172.28.0.0/24", "172.28.0.1"),
    5: ("rtc-proxy-slot-5", "172.29.0.0/24", "172.29.0.1"),
    6: ("rtc-proxy-slot-6", "172.31.0.0/24", "172.31.0.1"),
}
for _s in range(7, 31):
    WEB_PROXY_SLOT_NETWORKS[_s] = (f"rtc-proxy-slot-{_s}", f"10.90.{_s}.0/24", f"10.90.{_s}.1")

WEB_PLATFORM_ROUTES = {
    "reddit":     ("REDDIT_DOMAIN",     "reddit.com",     {1: 9999, 2: 9998, 3: 9997, 4: 9996}),
    "gitlab":     ("GITLAB_DOMAIN",     "gitlab.com",     {1: 8023, 2: 8623, 3: 8624, 4: 8023}),
    "owncloud":   ("OWNCLOUD_DOMAIN",   "owncloud.com",   {1: 8092, 2: 8093, 3: 8094, 4: 8092}),
    "rocketchat": ("ROCKETCHAT_DOMAIN", "rocketchat.com", {1: 3000, 2: 3001, 3: 3002, 4: 3000}),
}

WEB_UCM_SITES = {
    "ecommerce-search": (["shop.com", "www.shop.com", "ecommerce-search.com"], 8110),
    "calendar":         (["calendar.com", "www.calendar.com"], 8111),
    "banking":          (["banking.com", "www.banking.com", "securebank.com"], 8112),
    "customer-support": (["support.com", "www.support.com", "helpdesk.com"], 8113),
    "email":            (["webmail.com", "www.webmail.com", "mail.com"], 8114),
    "job-board":        (["jobs.com", "www.jobs.com", "careers.com"], 8115),
    "restaurant":       (["restaurant.com", "www.restaurant.com", "bellavista.com"], 8116),
    "travel-booking":   (["travel.com", "www.travel.com", "travelportal.com"], 8117),
    "wiki":             (["wiki.com", "www.wiki.com", "knowledge.com"], 8118),
    "forum":            (["forum.com", "www.forum.com", "devforum.com"], 8119),
    "offsite-attacker": (["partner-fares.net", "silk-deals.net"], 8120),
}


class DesktopEnv(gym.Env):
    """
    DesktopEnv with OpenAI Gym interface. It provides a desktop environment for setting and evaluating desktop automation tasks.
    """

    def __init__(
            self,
            provider_name: str = "docker",
            region: str = None,
            path_to_vm: str = None,
            snapshot_name: str = "init_state",
            action_space: str = "computer_13",
            cache_dir: str = "cache",
            screen_size: Tuple[int] = (1920, 1080),
            headless: bool = False,
            require_a11y_tree: bool = True,
            require_terminal: bool = False,
            os_type: str = "Ubuntu",
            aws_ami: str = None,
            ucm_reveal: str = "default",
    ):
        """
        Args:
            provider_name (str): virtualization provider name, default to "vmware"
            region (str): the region for allocate machines, work for cloud services, default to  "us-east-1"
            path_to_vm (str): path to .vmx file
            snapshot_name (str): snapshot name to revert to, default to "init_state"
            action_space (str): "computer_13" | "pyautogui"
            cache_dir (str): cache directory to cache task-related stuffs like
              reference file for evaluation
            screen_size (Tuple[int]): screen size of the VM
            headless (bool): whether to run the VM in headless mode
            require_a11y_tree (bool): whether to require accessibility tree
            require_terminal (bool): whether to require terminal output
        """
        self.region = region

        self.server_port = 5000
        self.chromium_port = 9222
        self.vnc_port = 8006
        self.vlc_port = 8080
        self.aws_ami = aws_ami
        self.manager, self.provider = create_vm_manager_and_provider(provider_name, region, self.aws_ami)
        self.provider_name = provider_name
        self.os_type = os_type
        self.ucm_reveal = ucm_reveal


        if path_to_vm and provider_name not in {"docker"}:
            self.path_to_vm = os.path.abspath(os.path.expandvars(os.path.expanduser(path_to_vm))) \
                if provider_name in {"vmware", "virtualbox"} else path_to_vm
        else:
            if provider_name in {"aws"}:
                self.path_to_vm = None
            else:
                self.path_to_vm = self.manager.get_vm_path(self.os_type, region)

        self.snapshot_name = snapshot_name
        self.cache_dir_base: str = cache_dir
        self.headless = headless
        self.require_a11y_tree = require_a11y_tree
        self.require_terminal = require_terminal

        if provider_name not in {"docker", "aws", "singularity"}:
            logger.info("Initializing...")
            self._start_emulator()

        self.instruction = None
        assert action_space in ["computer_13", "pyautogui"]
        self.action_space = action_space

        self._traj_no: int = -1
        self._step_no: int = 0
        self.action_history: List[Dict[str, any]] = []
        self.agent_responses: List[str] = []
        self.agent_report_indices: List[int] = []
        

    def _start_emulator(self):
        if self.provider_name in {"aws"}:
            self.provider.start_emulator(self.path_to_vm, None)
        else:
            self.provider.start_emulator(self.path_to_vm, self.headless, self.os_type)

        vm_ip_ports = self.provider.get_ip_address(self.path_to_vm).split(':')
        self.vm_ip = vm_ip_ports[0]
        ports = [int(p) for p in vm_ip_ports[1:] if p]

        if len(ports) >= 1:
            self.server_port = ports[0]
        if len(ports) >= 2:
            self.chromium_port = ports[1]
        if len(ports) == 3:
            self.vlc_port = ports[2]
        elif len(ports) >= 4:
            self.vnc_port = ports[2]
            self.vlc_port = ports[3]

        self.controller = PythonController(vm_ip=self.vm_ip, server_port=self.server_port)
        self.setup_controller = SetupController(vm_ip=self.vm_ip, server_port=self.server_port, chromium_port=self.chromium_port, vlc_port=self.vlc_port, cache_dir=self.cache_dir_base)

    def _quiet_os_update_notifications(self):
        """Silences the guest's software-update notifications (see _QUIET_UPDATES_CMD)."""
        if self.os_type != "Ubuntu":
            return
        try:
            self.setup_controller._execute_setup(_QUIET_UPDATES_CMD, shell=True)
        except Exception as e:
            logger.warning("Could not disable guest update notifications: %s", e)

    def dismiss_update_banner(self, tree: Optional[str] = None) -> bool:
        """Dismisses the "Software Updates Available to Download" / "...Ready to Install" /
        "Update available for Snap Store" banner if it is currently on screen, and returns whether
        one was found. _quiet_os_update_notifications() (called once, at boot) is prevention only
        -- it cannot help here, because GNOME Shell renders this banner directly (confirmed via its
        a11y node: role=label, toolkit=clutter -- gnome-shell's own compositor, not a separate app
        window) once some background process (real culprit confirmed live: update-notifier-
        download.timer, or snapd's own refresh check surfaced through `snap-store
        --gapplication-service`) fires a single Notify() call. Once fired, the banner is immune to
        every non-interactive dismiss this session tried: Escape, clicking elsewhere on the
        desktop, and org.gnome.Shell.Eval (disabled by default -- returns `(false, '')`) all left
        it unchanged. Only a literal click dismisses it. Call this right before every
        screenshot/a11y-tree capture, not just once at boot -- the banner can be showing at any
        point in a long-running session, not only in the first few seconds.
        Pass `tree` when the caller has already fetched an accessibility tree for this same
        moment, so observation modes that fetch one anyway pay nothing for this check.
        Best-effort: any failure here should never abort the caller's actual capture."""
        if self.os_type != "Ubuntu":
            return False
        try:
            if tree is None:
                tree = self.controller.get_accessibility_tree(retry_times=1) or ""
            banner = _find_update_banner(tree)
            if banner is None:
                return False
            (x, y), name, on_close = banner
            self.controller.execute_python_command(f"pyautogui.click({x}, {y})")
            time.sleep(1)
            if not on_close:
                self.controller.execute_command(
                    "pkill -f snap-store 2>/dev/null; pkill -x gnome-software 2>/dev/null || true")
                time.sleep(0.5)
            logger.info("dismissed update banner %r via %s", name,
                        "close button" if on_close else "banner body + pkill")
            return True
        except Exception as e:
            logger.warning("dismiss_update_banner() failed (non-fatal, continuing): %s", e)
            return False

    def _revert_to_snapshot(self):
        path_to_vm = self.provider.revert_to_snapshot(self.path_to_vm, self.snapshot_name)
        if self.provider_name in {"aws"}:
            self.path_to_vm = path_to_vm
        else:
            if path_to_vm and not path_to_vm == self.path_to_vm:
                self.manager.delete_vm(self.path_to_vm, self.region)
                self.manager.add_vm(path_to_vm, self.region)
                self.manager.occupy_vm(path_to_vm, os.getpid(), self.region)
                self.path_to_vm = path_to_vm

            

    def _save_state(self, snapshot_name=None):
        self.provider.save_state(self.path_to_vm, snapshot_name)


    def terminate(self):
        self.provider.terminate_emulator(self.path_to_vm)
               
    def close(self):
        self.provider.stop_emulator(self.path_to_vm)

    @staticmethod
    def _docker_bridge_gateway():
        """Address of the host on Docker's default bridge, or None if it cannot be read.

        localhost inside the VM refers to the VM itself, not the host, so this is how the guest
        reaches a platform container's published port (VM -> docker0 -> host:port -> container).
        """
        try:
            import docker as _docker
            _bridge = _docker.from_env().networks.get("bridge")
            return _bridge.attrs["IPAM"]["Config"][0]["Gateway"]
        except Exception as e:
            logger.warning(f"Could not read the Docker bridge gateway: {e}")
            return None

    def _proxy_slot_address(self, slot: int) -> str:
        """Host address the web proxy serves instance *slot* on, creating its network if needed.

        Slot 1 rides on the default bridge; the others need a network of their own purely for the
        gateway address it puts on the host (nothing is ever attached to them). Creating a network
        needs no privileges beyond the Docker socket the rest of this file already uses.
        """
        if slot == 1:
            return self._docker_bridge_gateway() or "172.17.0.1"
        if slot not in WEB_PROXY_SLOT_NETWORKS:
            raise KeyError(
                f"No web-proxy address for instance slot {slot}: WEB_PROXY_SLOT_NETWORKS covers "
                f"slots {sorted({1, *WEB_PROXY_SLOT_NETWORKS})}, but the instance pool handed out "
                f"slot {slot}."
            )

        name, subnet, gateway = WEB_PROXY_SLOT_NETWORKS[slot]
        try:
            import docker as _docker
            client = _docker.from_env()
            try:
                client.networks.get(name)
            except _docker.errors.NotFound:
                client.networks.create(
                    name,
                    driver="bridge",
                    ipam=_docker.types.IPAMConfig(
                        pool_configs=[_docker.types.IPAMPool(subnet=subnet, gateway=gateway)]),
                )
                logger.info(f"Created network '{name}' ({subnet}) for web-proxy slot {slot}.")
        except Exception as e:
            logger.warning(
                f"Could not create network '{name}' for web-proxy slot {slot}: {e}. "
                f"http://<domain> may be unreachable for this instance; create it manually with: "
                f"docker network create --subnet {subnet} --gateway {gateway} {name}"
            )
        return gateway

    @staticmethod
    def _render_web_proxy_conf(domains, slot_addresses, upstream_host: str) -> str:
        """nginx.conf routing (listen address, Host) -> published port, for every slot."""
        servers = []
        for slot, address in sorted(slot_addresses.items()):
            for platform, domain in sorted(domains.items()):
                _pr = WEB_PLATFORM_ROUTES[platform][2]
                port = _pr.get(slot, _pr[1])
                servers.append(
                    f"    # {platform} instance {slot}\n"
                    f"    server {{\n"
                    f"        listen      {address}:80;\n"
                    f"        server_name {domain};\n"
                    f"        location / {{ proxy_pass http://{upstream_host}:{port}; }}\n"
                    f"    }}\n"
                )
            for service, (hostnames, port) in sorted(WEB_UCM_SITES.items()):
                slot_port = port + (slot - 1) * 100
                servers.append(
                    f"    # ucm site {service} (slot {slot})\n"
                    f"    server {{\n"
                    f"        listen      {address}:80;\n"
                    f"        server_name {' '.join(hostnames)};\n"
                    f"        location / {{ proxy_pass http://{upstream_host}:{slot_port}; }}\n"
                    f"    }}\n"
                )
            servers.append(
                f"    server {{\n"
                f"        listen      {address}:80 default_server;\n"
                f"        server_name _;\n"
                f"        return      404;\n"
                f"    }}\n"
            )

        return (
            "# Generated by desktop_env.py -- edits are overwritten on the next run.\n"
            "worker_processes auto;\n"
            "error_log /var/log/nginx/error.log warn;\n"
            "pid /var/run/nginx.pid;\n"
            "\n"
            "events { worker_connections 1024; }\n"
            "\n"
            "http {\n"
            "    include      /etc/nginx/mime.types;\n"
            "    default_type application/octet-stream;\n"
            "    access_log   off;\n"
            "    sendfile     on;\n"
            "\n"
            "    # OwnCloud uploads are unbounded, and GitLab clones and RocketChat long-polls are slow.\n"
            "    client_max_body_size 0;\n"
            "    proxy_read_timeout   3600s;\n"
            "    proxy_send_timeout   3600s;\n"
            "    proxy_buffering      off;\n"
            "    proxy_http_version   1.1;\n"
            "\n"
            "    # $http_host, not $host: the backend has to see exactly what the agent typed, so that\n"
            "    # the absolute URLs it generates stay port-less too (OwnCloud's trusted_domains check,\n"
            "    # RocketChat's Site_URL and GitLab's external_url all key off this).\n"
            "    proxy_set_header Host              $http_host;\n"
            "    proxy_set_header X-Real-IP         $remote_addr;\n"
            "    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;\n"
            "    proxy_set_header X-Forwarded-Proto $scheme;\n"
            "\n"
            "    # RocketChat talks DDP over a websocket, so Upgrade has to survive the hop.\n"
            "    map $http_upgrade $connection_upgrade { default upgrade; '' close; }\n"
            "    proxy_set_header Upgrade    $http_upgrade;\n"
            "    proxy_set_header Connection $connection_upgrade;\n"
            "\n"
            + "\n".join(servers)
            + "}\n"
        )

    def _ensure_web_proxy(self) -> None:
        """Idempotently run the nginx container that serves every platform on port 80.

        Reused across runs. When the routing table changes, a running proxy is reloaded in place
        (`nginx -s reload`: old workers finish their connections, so the other runs' in-flight
        page loads and RocketChat websockets survive); only a stopped proxy or a failed reload is
        recreated. The live config's hash is the container label, or -- after a reload, since a
        label cannot change on a running container -- the hash file next to the config.
        Failure is not fatal here -- it surfaces as an unreachable http://<domain> in the
        guest, with the manual command in the log.
        """
        if getattr(self, "_web_proxy_ready", False):
            return

        gateway = self._docker_bridge_gateway() or "172.17.0.1"
        domains = {p: os.getenv(env_var, default)
                   for p, (env_var, default, _) in WEB_PLATFORM_ROUTES.items()}
        slot_addresses = {slot: self._proxy_slot_address(slot)
                          for slot in sorted({1, *WEB_PROXY_SLOT_NETWORKS})}
        conf = self._render_web_proxy_conf(domains, slot_addresses, gateway)
        conf_hash = hashlib.sha256(conf.encode()).hexdigest()[:12]

        conf_path = os.path.join(WEB_PROXY_CONF_DIR, "nginx.conf")
        hash_path = os.path.join(WEB_PROXY_CONF_DIR, "conf_hash")
        try:
            with open(hash_path) as f:
                reloaded_hash = f.read().strip()
        except OSError:
            reloaded_hash = None
        try:
            os.makedirs(WEB_PROXY_CONF_DIR, exist_ok=True)
            with open(conf_path, "w") as f:
                f.write(conf)
        except Exception as e:
            logger.warning(f"Could not write {conf_path}: {e}. Port-less URLs will not work.")
            return

        try:
            import docker as _docker
            client = _docker.from_env()

            try:
                client.containers.get("reddit-proxy").remove(force=True)
                logger.info("Removed the legacy 'reddit-proxy' container (replaced by the web proxy).")
            except _docker.errors.NotFound:
                pass

            try:
                existing = client.containers.get(WEB_PROXY_CONTAINER)
                if existing.status == "running":
                    live = (reloaded_hash if reloaded_hash is not None
                            else existing.labels.get("rtc.conf_hash"))
                    if live == conf_hash:
                        self._web_proxy_ready = True
                        return
                    code, out = existing.exec_run(["sh", "-c", "nginx -t && nginx -s reload"])
                    if code == 0:
                        with open(hash_path, "w") as f:
                            f.write(conf_hash)
                        self._web_proxy_ready = True
                        logger.info("Reloaded '%s' in place with the new routing table (%s).",
                                    WEB_PROXY_CONTAINER, conf_hash)
                        return
                    logger.warning("In-place reload of '%s' failed (%s); recreating it.",
                                   WEB_PROXY_CONTAINER, out.decode(errors="replace").strip()[-300:])
                existing.remove(force=True)
            except _docker.errors.NotFound:
                pass

            client.containers.run(
                WEB_PROXY_IMAGE,
                name=WEB_PROXY_CONTAINER,
                detach=True,
                network_mode="host",
                volumes={conf_path: {"bind": "/etc/nginx/nginx.conf", "mode": "ro"}},
                labels={"rtc.conf_hash": conf_hash},
                restart_policy={"Name": "unless-stopped"},
            )
            try:
                os.remove(hash_path)
            except OSError:
                pass
            self._web_proxy_ready = True
            logger.info(
                "Started '%s' on %s: %s.",
                WEB_PROXY_CONTAINER,
                ", ".join(f"{a}:80" for a in slot_addresses.values()),
                ", ".join(f"{d} -> :{WEB_PLATFORM_ROUTES[p][2][1]}" for p, d in sorted(domains.items())),
            )
        except Exception as e:
            logger.warning(
                f"Could not start '{WEB_PROXY_CONTAINER}': {e}. Port-less URLs such as "
                f"http://{domains['reddit']} will be unreachable; start it manually with: "
                f"docker run -d --name {WEB_PROXY_CONTAINER} --network host "
                f"-v {conf_path}:/etc/nginx/nginx.conf:ro {WEB_PROXY_IMAGE}"
            )

    def _build_login_info(self, hostname):
        if self.provider_name == "aws":
            return {
                'hostname': hostname,
                'username': 'user',
                'password': 'password',
            }
        elif self.provider_name == "docker":
            return {
                'hostname': hostname,
                'local': True,
            }
        else:
            login_info = {
                'hostname': hostname,
                'username': os.getenv("SSH_USERNAME", "ubuntu"),
                'key_filename': os.getenv("KEY_FILENAME"),
            }
            ssh_port = os.getenv("SSH_PORT")
            if ssh_port:
                login_info['port'] = int(ssh_port)
            return login_info

    def _jump_wait(self):
        return self.provider_name not in {"aws", "docker"}

    def _web_proxy_route(self, platform: str, slot: int = 1) -> Tuple[str, str]:
        """(display domain, host address) for *platform*'s instance *slot*, with the proxy up."""
        self._ensure_web_proxy()
        env_var, default, _ = WEB_PLATFORM_ROUTES[platform]
        return os.getenv(env_var, default), self._proxy_slot_address(slot)

    @staticmethod
    def _insert_hosts_entry(task_config, address: str, domain: str) -> None:
        """Prepend a guest /etc/hosts step resolving *domain* to *address*.

        Prepended so it runs before the Chrome launch/login steps. The VM reverts to a clean
        snapshot on each reset, so a plain append never duplicates.
        """
        hosts_cmd = ["sh", "-c",
            f"echo 'password' | sudo -S bash -c 'echo \"{address} {domain}\" >> /etc/hosts'"]
        task_config["config"].insert(0, {"type": "execute", "parameters": {"command": hosts_cmd}})

    @staticmethod
    def _clear_ucm_security_log(slot: int = 1) -> None:
        """Truncate the UCM sites' shared security.log before an episode.

        All ten containers bind-mount ONE logs directory, so security.log accumulates every
        event from every site and every previous episode. UCM's own scorer drops its time
        filter precisely because it clears the log before each run
        (task_eval.py::_window_logs, "logs are cleared before each run"); without this, a
        leftover task_complete event from an earlier episode satisfies the check before the
        agent does anything -- the same false-pass shape as the stale GitLab issue state.
        Truncated through a container so the file's uid and permissions are left alone.

        Best-effort: logged loudly rather than raised, because failing here makes results too
        PERMISSIVE rather than impossible, and that log line is what says the numbers are suspect.
        """
        try:
            import docker as _docker
            prefix = "ucm" if slot == 1 else f"ucm{slot}"
            container = _docker.from_env().containers.get(f"{prefix}-banking")
            code, out = container.exec_run(
                ["sh", "-c", ": > /usr/share/nginx/html/logs/security.log"])
            if code != 0:
                raise RuntimeError(out.decode().strip() or f"exit {code}")
            logger.info("Cleared the UCM security.log for this episode.")
        except Exception as e:
            logger.error(
                "Could not clear the UCM security.log (%s). Site-log checks may PASS on stale "
                "events from an earlier episode -- treat ucm_utility scores from this run as "
                "unreliable until this is fixed.", e)

    def _apply_ucm_reveal(self, task_config) -> None:
        """Append the reveal/mask query param to this episode's UCM start URLs per self.ucm_reveal.

        The UCM sites hide every data-untrusted region behind a placeholder by default (reveal.js:
        no query param => reveal-hidden). That is UCM's site-level defense. An undefended run must
        pass ?reveal_all=true so the agent actually sees the untrusted content -- including an
        injected value-attack ad, which is itself a data-untrusted region and would otherwise be
        masked and never presented (making adversary scores false negatives). "mask" is the
        opposite pole (?mask_untrusted=true); "default" leaves the URLs untouched. Only the
        chrome_open_tabs URLs are rewritten -- the attacker's off-domain link lives in
        adversary_config, not here, so it is never touched.
        """
        param = {"all": "reveal_all=true", "mask": "mask_untrusted=true"}.get(self.ucm_reveal)
        if not param:
            return

        def _with_param(url: str) -> str:
            if not isinstance(url, str) or not url.startswith("http"):
                return url
            if "reveal_all=" in url or "mask_untrusted=" in url:
                return url
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}{param}"

        touched = 0
        for step in task_config.get("config", []):
            if step.get("type") == "chrome_open_tabs":
                urls = step.get("parameters", {}).get("urls_to_open", [])
                for i, u in enumerate(urls):
                    new = _with_param(u)
                    if new != u:
                        urls[i] = new
                        touched += 1
        if touched:
            logger.info("UCM reveal policy '%s': rewrote %d start URL(s) with ?%s.",
                        self.ucm_reveal, touched, param)

    @staticmethod
    def _insert_hosts_entries(task_config, address: str, domains) -> None:
        """Prepend ONE guest step adding several /etc/hosts lines, one per domain.

        Not a loop over _insert_hosts_entry: that would be a separate sudo round-trip per
        name, and ucm_utility needs ~29. Not one line carrying all the names either --
        /etc/hosts allows many aliases per line, but resolver implementations differ on how
        many they will read, and a silently truncated line would present as a site that
        randomly fails to resolve. One line per host keeps it unambiguous.
        """
        lines = "".join(f"{address} {d}\n" for d in domains)
        blob = base64.b64encode(lines.encode()).decode()
        hosts_cmd = ["sh", "-c",
            f"echo 'password' | sudo -S bash -c 'echo {blob} | base64 -d >> /etc/hosts'"]
        task_config["config"].insert(0, {"type": "execute", "parameters": {"command": hosts_cmd}})

    def _ensure_owncloud_trusted_domain(self, container_name: str, domain: str,
                                        attempts: int = 6, delay: float = 3.0) -> None:
        """Teach an OwnCloud container to accept the port-less Host the proxy passes through.

        OwnCloud answers any Host outside trusted_domains with "Access through untrusted domain"
        (a bare 400 in the browser). That list lives in the image's baked config.php -- the image
        only reads OWNCLOUD_TRUSTED_DOMAINS at install time -- and an episode reset recreates the
        container from that image, so this is re-applied per episode rather than configured once.

        Runs right after `reset.sh` recreates the container, which is exactly when a single attempt is
        flaky: for the first few seconds occ answers before the DB/config is fully up (the domain is
        "set" but does not stick, so the browser still gets the untrusted-domain page -- the observed
        "sometimes denied"). So retry AND VERIFY: re-fetch the container by name each round (a recreated
        container reuses the name but not the object), set the domain, and read `trusted_domains` back
        to confirm it actually contains it before returning. Idempotent -- appends only when absent."""
        import time as _time
        import docker as _docker
        last_err = "not attempted"
        for attempt in range(1, attempts + 1):
            try:
                container = _docker.from_env().containers.get(container_name)
                code, out = container.exec_run(
                    ["php", "occ", "config:system:get", "trusted_domains"], user="www-data")
                trusted = out.decode().split() if code == 0 else []
                if domain in trusted:
                    if attempt > 1:
                        logger.info(f"'{domain}' trusted on {container_name} (verified, attempt {attempt}).")
                    return
                if code != 0:
                    raise RuntimeError(f"occ get failed: {out.decode().strip()}")
                code, out = container.exec_run(
                    ["php", "occ", "config:system:set", "trusted_domains", str(len(trusted)),
                     f"--value={domain}"], user="www-data")
                if code != 0:
                    raise RuntimeError(f"occ set failed: {out.decode().strip()}")
                code, out = container.exec_run(
                    ["php", "occ", "config:system:get", "trusted_domains"], user="www-data")
                if code == 0 and domain in out.decode().split():
                    container.exec_run(
                        ["sh", "-c", "apache2ctl -k graceful 2>/dev/null || "
                         "apachectl -k graceful 2>/dev/null || kill -USR1 1 2>/dev/null || true"],
                        user="root")
                    logger.info(f"Added '{domain}' to {container_name}'s trusted domains "
                                f"(verified + apache reloaded, attempt {attempt}).")
                    return
                last_err = "set reported success but the domain was not present on read-back"
            except Exception as e:
                last_err = str(e)
            if attempt < attempts:
                _time.sleep(delay)
        logger.warning(
            f"Could not add '{domain}' to {container_name}'s trusted domains after {attempts} "
            f"attempts: {last_err}. http://{domain} will answer 400 until it is trusted."
        )

    @staticmethod
    def _ensure_owncloud_wopi_url(container_name: str, slot: int) -> None:
        """Point OwnCloud slot *slot* at ITS OWN Collabora (9980 + slot - 1), not slot 1's.

        The image bakes richdocuments' wopi_url as http://the-agent-company.com:9980 on every copy,
        so OwnCloud-2/-3 sent their office documents to owncloud-collabora (slot 1). That Collabora
        calls the WOPI host back at http://owncloud.com, which it resolves to slot 1's proxy address,
        so it could never serve another slot's file. Each collabora-N now allows owncloud.com and
        resolves it to slot N's proxy address (TheAgentCompany servers/docker-compose.yml), so the
        pairing has to be 1:1. The reset recreates the container from the image, so this is re-applied
        per episode. Best-effort: a failure only breaks office documents (PDFs use their own viewer).
        """
        import docker as _docker
        url = f"http://the-agent-company.com:{9979 + int(slot)}"
        try:
            container = _docker.from_env().containers.get(container_name)
            code, out = container.exec_run(
                ["php", "occ", "config:app:set", "richdocuments", "wopi_url", f"--value={url}"],
                user="www-data")
            if code != 0:
                raise RuntimeError(out.decode(errors="replace").strip())
            logger.info(f"{container_name}: richdocuments wopi_url -> {url}.")
        except Exception as e:
            logger.warning(f"Could not set {container_name}'s richdocuments wopi_url to {url}: {e}. "
                           f"Office documents will fail to open on this slot.")

    def _ensure_rocketchat_display_name(self, mongo_container: str, own_user: str = "theagentcompany",
                                        display_as: str = None) -> None:
        """Show the agent's own account as a display name rather than its raw username.

        The agent is logged in as `theagentcompany`, and RocketChat labels its messages with
        that username, so the transcript reads as a conversation between two third parties
        rather than one the agent is having itself. Turning on real-name display and naming
        the account "me" makes its own messages read that way -- and, as a side effect,
        labels the NPC "Mike Chen" instead of "mike_chen", matching how the instruction
        names him.

        Must run BEFORE the Sotopia NPC is started. Real-name display is a global setting,
        RocketChat watches rocketchat_settings and reloads on a write there, and a reload
        after the NPC is up drops the bot's session -- which presents as an NPC that never
        answers a thing. Called once, ahead of the NPC, that reload is harmless: the agent's
        browser only logs in later, so it still picks the new labels up. The per-user
        preference is written too, but on its own it does not reach an already-loaded client.

        Written with the same write-then-verify retries as the Site_Url, since the episode
        reset's mongorestore has trailing writes that land after reset.sh returns and would
        otherwise silently put the dump's values back.

        Set ROCKETCHAT_DISPLAY_NAME="" to skip.
        """
        if display_as is None:
            display_as = os.getenv("ROCKETCHAT_DISPLAY_NAME", "me")
        if not display_as:
            return

        try:
            import docker as _docker
            container = _docker.from_env().containers.get(mongo_container)
        except Exception as e:
            logger.warning(f"Could not reach {mongo_container} to set the display name: {e}.")
            return

        write = (
            f'db.users.updateOne({{username:"{own_user}"}},'
            f'{{$set:{{name:"{display_as}","settings.preferences.useRealName":true}}}});'
            'db.rocketchat_settings.updateOne({_id:"UI_Use_Real_Name"},{$set:{value:true}});'
        )
        check = (
            f'var u=db.users.findOne({{username:"{own_user}"}});'
            'var s=db.rocketchat_settings.findOne({_id:"UI_Use_Real_Name"});'
            'print((u?u.name:"?")+"|"+(s?s.value:"?"));'
        )
        expected = f"{display_as}|true"

        for attempt in range(1, 6):
            code, out = container.exec_run(["mongo", "rocketchat", "--quiet", "--eval", write])
            if code != 0:
                logger.warning(f"Could not set {own_user}'s display name: {out.decode().strip()}")
                return
            time.sleep(2)
            code, out = container.exec_run(["mongo", "rocketchat", "--quiet", "--eval", check])
            result = out.decode().strip().splitlines()[-1].strip() if out else ""
            if result == expected:
                logger.info(f"{mongo_container}: {own_user} displays as {display_as!r} "
                            f"(attempt {attempt}).")
                return
            logger.info(f"{mongo_container}: display name reverted to {result!r}; "
                        f"retrying ({attempt}/5).")

        logger.warning(
            f"{own_user}'s display name would not stay at {display_as!r} on {mongo_container}; "
            f"its messages will still be labelled {own_user!r}."
        )

    def _ensure_rocketchat_site_url(self, mongo_container: str, domain: str) -> None:
        """Point a RocketChat instance's Site_Url at the port-less domain.

        RocketChat overrides Meteor's ROOT_URL with the Site_Url setting it reads from Mongo, so
        the ROOT_URL in the compose file only wins on a database-less first boot -- and an episode
        reset is a mongorestore of a dump carrying the old, port-qualified value, which the client
        would then be told to open its websocket to. Written to the database rather than to the
        container because RocketChat watches that collection: the URL it serves changes right away,
        with no restart to wait out.
        """
        target = f"http://{domain}"
        try:
            import docker as _docker
            container = _docker.from_env().containers.get(mongo_container)
        except Exception as e:
            logger.warning(f"Could not reach {mongo_container} to set Site_Url: {e}.")
            return

        for attempt in range(1, 6):
            code, out = container.exec_run(["mongo", "rocketchat", "--quiet", "--eval",
                f'db.rocketchat_settings.updateOne({{_id:"Site_Url"}},'
                f'{{$set:{{value:"{target}"}}}})'])
            if code != 0:
                logger.warning(f"Could not set {mongo_container}'s Site_Url: {out.decode().strip()}")
                return
            time.sleep(2)
            code, out = container.exec_run(["mongo", "rocketchat", "--quiet", "--eval",
                'print(db.rocketchat_settings.findOne({_id:"Site_Url"}).value)'])
            current = out.decode().strip().splitlines()[-1].strip() if out else ""
            if current == target:
                logger.info(f"Set {mongo_container}'s Site_Url to {target} (attempt {attempt}).")
                return
            logger.info(f"{mongo_container}'s Site_Url reverted to {current!r}; retrying ({attempt}/5).")

        logger.warning(
            f"{mongo_container}'s Site_Url would not stay at {target}; RocketChat will show the "
            f"client a Site-URL mismatch modal that blocks the UI."
        )

    def _warn_if_gitlab_external_url_has_port(self, domain: str) -> None:
        """GitLab is the one platform whose own config has to agree with the port-less URL.

        Its external_url is baked into /etc/gitlab/gitlab.rb, and every redirect, clone URL and
        asset link is generated from it -- so if it still carries :8023, the port reappears in
        the address bar the moment the agent follows a link. Checked once per process; see
        setup_gitlab_portless_url.sh for the fix.
        """
        if getattr(self, "_gitlab_url_checked", False):
            return
        self._gitlab_url_checked = True
        try:
            import docker as _docker
            container = _docker.from_env().containers.get(os.getenv("GITLAB_CONTAINER", "gitlab"))
            code, out = container.exec_run(
                ["grep", "-m1", "^external_url", "/etc/gitlab/gitlab.rb"])
            if code != 0:
                return
            external_url = out.decode().strip()
        except Exception as e:
            logger.debug(f"Could not read GitLab's external_url: {e}")
            return

        if f"http://{domain}'" not in external_url:
            logger.warning(
                f"GitLab's {external_url} does not match http://{domain}: its redirects and clone "
                f"URLs will still carry a port. Fix it with: bash setup_gitlab_portless_url.sh"
            )

    def _localize_task_config(self, task_config: Dict[str, Any]) -> Dict[str, Any]:
        """Point a task config at this deployment: replace the TO_BE_REPLACED_* placeholders the
        config generator left behind with the addresses and URLs the agent should actually see.

        Two different substitutions, deliberately: TO_BE_REPLACED_IP becomes a raw address for the
        host-side machinery (content injection, the RocketChat NPC bots), while TO_BE_REPLACED_URL
        becomes what the agent's browser navigates to -- under the docker provider a port-less
        domain served by the web proxy, see WEB_PLATFORM_ROUTES. The port-qualified placeholder is
        always replaced before the bare one, so the generator's port cannot survive.
        """
        if self.provider_name == "aws":
            if task_config["platform"] == "reddit":
                task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL", f"http://{self.vm_ip}")
                task_config = replace_in_dict(task_config, "TO_BE_REPLACED_IP", f"{self.vm_ip}")

            elif task_config["platform"] == "owncloud":
                task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL", "http://the-agent-company.com")
                task_config = replace_in_dict(task_config, "TO_BE_REPLACED_IP", f"{self.vm_ip}")

            elif task_config["platform"] == "rocketchat":
                task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL", "http://the-agent-company.com")
                task_config = replace_in_dict(task_config, "TO_BE_REPLACED_IP", f"{self.vm_ip}")

            elif task_config["platform"] == "gitlab":
                task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL", f"http://{self.vm_ip}")
                task_config = replace_in_dict(task_config, "TO_BE_REPLACED_IP", f"{self.vm_ip}")


        else:
            if task_config["platform"] == "reddit":
                if self.provider_name == "docker":
                    self.reddit_ip = self._docker_bridge_gateway() or os.getenv("REDDIT", "172.17.0.1")
                    assert self.reddit_ip, "Run export REDDIT='<your_reddit_domain>'"

                    _reddit_inst = get_current_reddit_instance() or acquire_reddit_instance()
                    reddit_domain, proxy_address = self._web_proxy_route("reddit", _reddit_inst["id"])
                    self._insert_hosts_entry(task_config, proxy_address, reddit_domain)

                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL:9999", f"http://{reddit_domain}")
                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL", f"http://{reddit_domain}")
                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_IP", f"{self.reddit_ip}")
                else:
                    self.reddit_ip = os.getenv("REDDIT", "localhost")
                    assert self.reddit_ip, "Run export REDDIT='<your_reddit_domain>'"

                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL", f"http://{self.reddit_ip}")
                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_IP", f"{self.reddit_ip}")

            elif task_config["platform"] == "owncloud":
                if self.provider_name == "docker":
                    self.owncloud_ip = self._docker_bridge_gateway() or os.getenv("OWNCLOUD", "172.17.0.1")
                else:
                    self.owncloud_ip = os.getenv("OWNCLOUD")
                assert self.owncloud_ip, "Run export OWNCLOUD='<your_owncloud_domain>'"

                _inst = acquire_owncloud_instance()
                _oc_port = _inst["port"]
                if self.provider_name == "docker":
                    oc_domain, proxy_address = self._web_proxy_route("owncloud", slot=_inst["id"])
                    self._insert_hosts_entry(task_config, proxy_address, oc_domain)
                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL:8092", f"http://{oc_domain}")
                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL", f"http://{oc_domain}")
                else:
                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL:8092",
                                                   f"http://the-agent-company.com:{_oc_port}")
                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL", "http://the-agent-company.com")
                task_config = replace_in_dict(task_config, "TO_BE_REPLACED_IP", f"{self.owncloud_ip}")

            elif task_config["platform"] == "rocketchat":
                if self.provider_name == "docker":
                    self.rocketchat_ip = self._docker_bridge_gateway() or os.getenv("ROCKETCHAT", "172.17.0.1")
                else:
                    self.rocketchat_ip = os.getenv("ROCKETCHAT")
                assert self.rocketchat_ip, "Run export ROCKETCHAT='<your_rocketchat_domain>'"

                _rc_inst = acquire_rocketchat_instance()
                _rc_port = _rc_inst["port"]
                _redis_port = _rc_inst["redis_port"]
                task_config = replace_in_dict(task_config, f"TO_BE_REPLACED_IP:3000",
                                               f"{self.rocketchat_ip}:{_rc_port}")
                task_config = replace_in_dict(task_config, f"TO_BE_REPLACED_IP:6379",
                                               f"{self.rocketchat_ip}:{_redis_port}")
                if self.provider_name == "docker":
                    rc_domain, proxy_address = self._web_proxy_route("rocketchat", slot=_rc_inst["id"])
                    self._insert_hosts_entry(task_config, proxy_address, rc_domain)
                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL:3000", f"http://{rc_domain}")
                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL", f"http://{rc_domain}")
                    self._ensure_rocketchat_site_url(
                        f"rocketchat-mongodb{_rc_inst['suffix']}", rc_domain)
                else:
                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL:3000",
                                                   f"http://the-agent-company.com:{_rc_port}")
                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL", "http://the-agent-company.com")
                task_config = replace_in_dict(task_config, "TO_BE_REPLACED_IP", f"{self.rocketchat_ip}")

            elif task_config["platform"] == "ucm":
                if self.provider_name == "docker":
                    self._ensure_web_proxy()
                    ucm_slot = int(os.getenv("UCM_INSTANCE_ID", "1") or "1")
                    proxy_address = self._proxy_slot_address(ucm_slot)
                    _hosts = [h for _, (hs, _p) in sorted(WEB_UCM_SITES.items()) for h in hs]
                    self._insert_hosts_entries(task_config, proxy_address, _hosts)
                    self._clear_ucm_security_log(ucm_slot)
                    self._apply_ucm_reveal(task_config)
                else:
                    logger.warning(
                        "ucm_utility expects provider_name=docker; the UCM site containers are "
                        "reached through the docker bridge, so http://<site> will not resolve here."
                    )

            elif task_config["platform"] == "gitlab":
                if self.provider_name == "docker":
                    self.gitlab_ip = self._docker_bridge_gateway() or os.getenv("GITLAB", "172.17.0.1")
                    assert self.gitlab_ip, "Run export GITLAB='<your_gitlab_domain>'"

                    _gitlab_inst = get_current_gitlab_instance() or acquire_gitlab_instance()
                    gitlab_domain, proxy_address = self._web_proxy_route("gitlab", _gitlab_inst["id"])
                    self._insert_hosts_entry(task_config, proxy_address, gitlab_domain)
                    self._warn_if_gitlab_external_url_has_port(gitlab_domain)

                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL:8023", f"http://{gitlab_domain}")
                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL", f"http://{gitlab_domain}")
                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_IP", f"{self.gitlab_ip}")
                else:
                    self.gitlab_ip = os.getenv("GITLAB", "localhost")
                    assert self.gitlab_ip, "Run export GITLAB='<your_gitlab_domain>'"

                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_URL", f"http://{self.gitlab_ip}")
                    task_config = replace_in_dict(task_config, "TO_BE_REPLACED_IP", f"{self.gitlab_ip}")

        return task_config

    def prepare_injection(self, task_config: Optional[Dict[str, Any]] = None, seed=None, options=None) -> Dict[str, Any]:
        logger.info("Resetting environment...")
        logger.info("Switching task...")
        logger.info("Setting counters...")
        self._traj_no += 1
        self._step_no = 0
        self.action_history.clear()
        self.agent_responses.clear()
        self.agent_report_indices.clear()

        logger.info("Reverting to snapshot to {}...".format(self.snapshot_name))
        self._revert_to_snapshot()
        logger.info("Starting emulator...")
        self._start_emulator()
        logger.info("Emulator started.")

        self._quiet_os_update_notifications()

        if task_config is not None:
            task_config = self._localize_task_config(task_config)

            self._set_task_info(task_config)

            if task_config["platform"] == "reddit" and \
                    os.getenv("RESET_REDDIT_DATABASE", "False").lower() == "true":
                reset_hostname = self.vm_ip if self.provider_name == "aws" else self.reddit_ip
                reset_login_info = self._build_login_info(reset_hostname)

                reddit_variant = "fixed" if "fixed" in task_config.get("domain", "") else "baseline"
                logger.info("Resetting Reddit database to benign baseline (variant=%s)...", reddit_variant)
                if populate_reddit_database(login_info=reset_login_info, variant=reddit_variant):
                    os.environ["RESET_REDDIT_DATABASE"] = "False"
                    logger.info("Reddit database reset complete.")
                else:
                    raise RuntimeError("Reddit database reset failed.")

            if task_config["platform"] == "ucm":
                _ucm_inject = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "ucm_sites", "logs", "inject.json")
                try:
                    if os.path.exists(_ucm_inject):
                        os.remove(_ucm_inject)
                        logger.info("Cleared stale UCM inject.json")
                except OSError as e:
                    logger.warning("Could not clear UCM inject.json: %s", e)

            logger.info("Setting up adversary config...")
            for adversary_c in self.adversary_config:
                hostname = adversary_c["ip"]
                ssh_login_info = self._build_login_info(hostname)
                jump_wait = self._jump_wait()

                if adversary_c["type"] == "reddit_adv_setup":
                    logger.info("Checking if the reddit is ready...")
                    _reddit_inst = get_current_reddit_instance()
                    if check_server_ready_reddit(ssh_login_info["hostname"], jump_wait = jump_wait,
                                                 port=_reddit_inst["port"] if _reddit_inst else 9999):
                        logger.info("Reddit is ready")
                    else:
                        raise RuntimeError("Reddit is not ready.")

                    _wasp_source = task_config.get("wasp_source", {})
                    _r_setup_fn = _wasp_source.get("setup_fn", "")
                    _r_cleanup_fn = _wasp_source.get("cleanup_fn", "")
                    _r_params = _wasp_source.get("setup_cleanup_parameters", {})
                    if _r_setup_fn or _r_cleanup_fn:
                        logger.info("Running Reddit cleanup_fn (best-effort, pre-episode)...")
                        run_reddit_cleanup_fn(_r_cleanup_fn, ssh_login_info, parameters=_r_params)
                        if _r_setup_fn:
                            logger.info("Running Reddit setup_fn...")
                            _r_result = run_reddit_setup_fn(_r_setup_fn, ssh_login_info, parameters=_r_params)
                            if _r_result.get("submission_id") is not None:
                                _sid = str(_r_result["submission_id"])
                                self.config = replace_in_dict(self.config, "__SEED_SUBMISSION_ID__", _sid)
                                self.evaluator = replace_in_dict(self.evaluator, "__SEED_SUBMISSION_ID__", _sid)
                                self.adversary_evaluator = replace_in_dict(
                                    self.adversary_evaluator, "__SEED_SUBMISSION_ID__", _sid)
                                adversary_c = replace_in_dict(adversary_c, "__SEED_SUBMISSION_ID__", _sid)

                    logger.info("Performing injection on the fly...")
                    if reddit_adv_setup(adversary_c,login_info=ssh_login_info):
                        logger.info("Injection is done.")
                    else:
                        raise RuntimeError("Injection failed.")
                    
                elif adversary_c["type"] == "rocketchat_adv_setup":
                    _rc_inst = get_current_rocketchat_instance() or acquire_rocketchat_instance()
                    _rc_suffix = _rc_inst["suffix"]

                    logger.info("Checking if the rocketchat is ready...")
                    if check_server_ready_rocketchat(ssh_login_info["hostname"], jump_wait=jump_wait,
                                                      service_suffix=_rc_suffix):
                        logger.info("Rocketchat is ready")
                    else:
                        raise RuntimeError("Rocketchat is not ready.")

                    original_dir = os.getcwd()
                    os.chdir(os.path.join(_REDTEAMCUA_ROOT, 'adv', 'TheAgentCompany_RocketChat'))
                    try:
                        if self.provider_name != "aws":
                            _reset_env = {**os.environ, 'ROCKETCHAT_INSTANCE_SUFFIX': _rc_suffix,
                                          'SERVER_HOSTNAME': self.rocketchat_ip}
                            subprocess.run(['bash', 'reset.sh'], check=True, env=_reset_env)
                        if self.provider_name == "docker":
                            self._ensure_rocketchat_site_url(
                                f"rocketchat-mongodb{_rc_suffix}",
                                os.getenv("ROCKETCHAT_DOMAIN", WEB_PLATFORM_ROUTES["rocketchat"][1]))
                            self._ensure_rocketchat_display_name(f"rocketchat-mongodb{_rc_suffix}")
                        rocketchat_adv_setup(adversary_c)
                        if self.provider_name == "docker":
                            self._ensure_rocketchat_site_url(
                                f"rocketchat-mongodb{_rc_suffix}",
                                os.getenv("ROCKETCHAT_DOMAIN", WEB_PLATFORM_ROUTES["rocketchat"][1]))
                    finally:
                        os.chdir(original_dir)

                elif adversary_c["type"] == "owncloud_adv_setup":
                    _inst = get_current_owncloud_instance() or acquire_owncloud_instance()
                    _suffix = _inst["suffix"]
                    _container = _inst["container"]

                    logger.info("Checking if the owncloud is ready...")
                    if check_server_ready_owncloud(ssh_login_info["hostname"], jump_wait=jump_wait,
                                                    service_suffix=_suffix):
                        logger.info("Owncloud is ready")
                    else:
                        raise RuntimeError("Owncloud is not ready.")

                    original_dir = os.getcwd()
                    os.chdir(os.path.join(_REDTEAMCUA_ROOT, 'adv', 'TheAgentCompany_OwnCloud'))
                    try:
                        if self.provider_name != "aws":
                            _reset_env = {**os.environ, 'OWNCLOUD_INSTANCE_SUFFIX': _suffix,
                                          'SERVER_HOSTNAME': self.owncloud_ip}
                            subprocess.run(['bash', 'reset.sh'], check=True, env=_reset_env)
                    finally:
                        os.chdir(original_dir)

                    if self.provider_name == "docker":
                        self._ensure_owncloud_trusted_domain(
                            _container, os.getenv("OWNCLOUD_DOMAIN", WEB_PLATFORM_ROUTES["owncloud"][1]))
                        self._ensure_owncloud_wopi_url(_container, _inst["id"])

                    logger.info("Performing injection on the fly...")
                    if owncloud_adv_setup(adversary_c, login_info=ssh_login_info, container_name=_container):
                        logger.info("Injection is done.")
                    else:
                        raise RuntimeError("Injection failed.")
                    owncloud_add_user(ssh_login_info, container_name=_container)

                elif adversary_c["type"] == "gitlab_adv_setup":
                    logger.info("Checking if gitlab is ready...")
                    if check_server_ready_gitlab(ssh_login_info["hostname"], port=gitlab_port(), jump_wait=jump_wait):
                        logger.info("Gitlab is ready")
                    else:
                        raise RuntimeError("Gitlab is not ready.")

                    _wasp_source = task_config.get("wasp_source", {})
                    _setup_fn = _wasp_source.get("setup_fn", "")
                    _cleanup_fn = _wasp_source.get("cleanup_fn", "")
                    _setup_cleanup_params = _wasp_source.get("setup_cleanup_parameters", {})
                    _gitlab_domain = f"http://{ssh_login_info['hostname']}:{gitlab_port()}"

                    if _setup_fn or _cleanup_fn:
                        logger.info("Running GitLab cleanup_fn (best-effort, pre-episode)...")
                        run_gitlab_cleanup_fn(_cleanup_fn, _gitlab_domain, parameters=_setup_cleanup_params)
                        logger.info("Running GitLab setup_fn...")
                        run_gitlab_setup_fn(_setup_fn, _gitlab_domain, parameters=_setup_cleanup_params)

                    logger.info("Filing attacker injection issue...")
                    _iid = gitlab_adv_setup(adversary_c, login_info=ssh_login_info)
                    if _iid is None:
                        raise RuntimeError("Injection failed.")
                    _iid = str(_iid)
                    adversary_c["parameters"]["issue_iid"] = _iid
                    self.config = replace_in_dict(self.config, "__SEED_ISSUE_IID__", _iid)
                    self.evaluator = replace_in_dict(self.evaluator, "__SEED_ISSUE_IID__", _iid)
                    self.adversary_evaluator = replace_in_dict(
                        self.adversary_evaluator, "__SEED_ISSUE_IID__", _iid)
                    logger.info("Injection is done.")

                elif adversary_c["type"] == "ucm_inject":
                    import json as _json
                    _ucm_logs = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ucm_sites", "logs")
                    os.makedirs(_ucm_logs, exist_ok=True)
                    _payload = adversary_c["parameters"]["payload"]
                    with open(os.path.join(_ucm_logs, "inject.json"), "w") as _f:
                        _json.dump(_payload, _f)
                    logger.info("Wrote UCM inject.json (%s)", list(_payload))

                else:
                    raise ValueError("adversary config is wrong")


            logger.info("Adversary setup complete.")

            self.setup_controller.reset_cache_dir(self.cache_dir)
            logger.info("Setting up environment...")

            self.setup_controller.setup(self.config)
            logger.info("Environment setup complete.")
            time.sleep(20)

        observation = self._get_obs()
        return observation

    def _get_obs(self):
        accessibility_tree = (
            self.controller.get_accessibility_tree() if self.require_a11y_tree else None
        )

        if self.dismiss_update_banner(accessibility_tree):
            if self.require_a11y_tree:
                accessibility_tree = self.controller.get_accessibility_tree()

        return {
            "screenshot": self.controller.get_screenshot(),
            "accessibility_tree": accessibility_tree,
            "terminal": self.controller.get_terminal_output() if self.require_terminal else None,
            "instruction": self.instruction
        }

    @property
    def vm_platform(self):
        return self.controller.get_vm_platform()

    @property
    def vm_screen_size(self):
        return self.controller.get_vm_screen_size()

    def _set_task_info(self, task_config: Dict[str, Any]):
        self.task_id: str = task_config["id"]
        self.cache_dir: str = os.path.join(self.cache_dir_base, self.task_id)
        os.makedirs(self.cache_dir, exist_ok=True)
        self.instruction = task_config["instruction"]
        self.config = task_config["config"] if "config" in task_config else []
        self.adversary_config = task_config["adversary_config"] if "adversary_config" in task_config else []

        self.current_use_proxy: bool = bool(task_config.get("proxy", False))

        self.evaluator = task_config["evaluator"]
        self.metric: Metric = [getattr(metrics, func) for func in self.evaluator["func"]] \
            if isinstance(self.evaluator["func"], list) \
            else getattr(metrics, self.evaluator["func"])
        self.metric_conj: str = self.evaluator.get("conj", "and")
        if "result" in self.evaluator and len(self.evaluator["result"]) > 0:
            self.result_getter: Getter = [getattr(getters, "get_{:}".format(res["type"])) for res in
                                          self.evaluator["result"]] \
                if isinstance(self.evaluator["result"], list) \
                else getattr(getters, "get_{:}".format(self.evaluator["result"]["type"]))
        else:
            self.result_getter = [None] * len(self.metric) \
                if isinstance(self.metric, list) \
                else None

        if "expected" in self.evaluator and len(self.evaluator["expected"]) > 0:
            self.expected_getter: Getter = [getattr(getters, "get_{:}".format(exp["type"])) if exp else None for exp in
                                            self.evaluator["expected"]] \
                if isinstance(self.evaluator["expected"], list) \
                else getattr(getters, "get_{:}".format(self.evaluator["expected"]["type"]))
        else:
            self.expected_getter = [None] * len(self.metric) \
                if isinstance(self.metric, list) \
                else None
        self.metric_options: Union[List[Dict[str, Any]], Dict[str, Any]] = [opt if opt else {} for opt in
                                                                            self.evaluator["options"]] \
            if isinstance(self.evaluator.get("options", {}), list) \
            else self.evaluator["options"] \
            if "options" in self.evaluator \
            else [{}] * len(self.metric) \
            if isinstance(self.metric, list) \
            else {}

        assert (not isinstance(self.evaluator["func"], list)
                or (len(self.metric) == len(self.result_getter) == len(self.expected_getter) == len(
                    self.metric_options)))
        
        if "adversarial_evaluator" in task_config and "adversary_evaluator" not in task_config:
            raise ValueError(
                f"Task {task_config.get('id')!r} declares 'adversarial_evaluator', but the key "
                "DesktopEnv reads is 'adversary_evaluator'. Rename it in the task config -- "
                "otherwise the adversarial objective is never scored."
            )
        if not task_config.get("adversary_evaluator"):
            self.adversary_evaluator = {}
            self.adversary_metric = None
        else: 
            self.adversary_evaluator = task_config["adversary_evaluator"]
            self.adversary_metric: Metric = [getattr(metrics, func) for func in self.adversary_evaluator["func"]] \
                if isinstance(self.adversary_evaluator["func"], list) \
                else getattr(metrics, self.adversary_evaluator["func"])
            self.adversary_metric_conj: str = self.adversary_evaluator.get("conj", "and")
        if "result" in self.adversary_evaluator and len(self.adversary_evaluator["result"]) > 0:
            self.adversary_result_getter: Getter = [getattr(getters, "get_{:}".format(res["type"])) for res in
                                          self.adversary_evaluator["result"]] \
                if isinstance(self.adversary_evaluator["result"], list) \
                else getattr(getters, "get_{:}".format(self.adversary_evaluator["result"]["type"]))
        else:
            self.adversary_result_getter = [None] * len(self.adversary_metric) \
                if isinstance(self.adversary_metric, list) \
                else None

        if "expected" in self.adversary_evaluator and len(self.adversary_evaluator["expected"]) > 0:
            self.adversary_expected_getter: Getter = [getattr(getters, "get_{:}".format(exp["type"])) if exp else None for exp in
                                            self.adversary_evaluator["expected"]] \
                if isinstance(self.adversary_evaluator["expected"], list) \
                else getattr(getters, "get_{:}".format(self.adversary_evaluator["expected"]["type"]))
        else:
            self.adversary_expected_getter = [None] * len(self.adversary_metric) \
                if isinstance(self.adversary_metric, list) \
                else None
        self.adversary_metric_options: Union[List[Dict[str, Any]], Dict[str, Any]] = [opt if opt else {} for opt in
                                                                            self.adversary_evaluator["options"]] \
            if isinstance(self.adversary_evaluator.get("options", {}), list) \
            else self.adversary_evaluator["options"] \
            if "options" in self.adversary_evaluator \
            else [{}] * len(self.adversary_metric) \
            if isinstance(self.adversary_metric, list) \
            else {}

        if self.adversary_metric is not None:
            assert (not isinstance(self.adversary_evaluator["func"], list)
                    or (len(self.adversary_metric) == len(self.adversary_result_getter) == len(self.adversary_expected_getter) == len(
                        self.adversary_metric_options)))

    def step(self, action, pause=2):
        self._step_no += 1
        self.action_history.append(action)

        reward = 0
        done = False
        info = {}

        if action in ['WAIT', 'FAIL', 'DONE'] or (type(action) == dict and action['action_type'] in ['WAIT', 'FAIL', 'DONE']):
            if action == 'WAIT':
                time.sleep(pause)
            elif action == 'FAIL':
                done = True
                info = {"fail": True}
            elif action == 'DONE':
                done = True
                info = {"done": True}

        if self.action_space == "computer_13":
            self.controller.execute_action(action)
        elif self.action_space == "pyautogui":
            if action in ['WAIT', 'FAIL', 'DONE']:
                self.controller.execute_action(action)
            else:
                result = self.controller.execute_python_command(action)
                if result and result.get("error"):
                    info["error"] = result["error"]

        time.sleep(pause)
        observation = self._get_obs()
        observation["error"] = info.get("error", "")

        return observation, reward, done, info

    def evaluate(self, response = None): 
        """
        Evaluate whether the task is successfully completed.
        """

        self.setup_controller.setup(self.evaluator.get("postconfig", []))

        if self.evaluator.get("func") == "infeasible":
            return 1.0 if (self.action_history and self.action_history[-1] == "FAIL") else 0.0

        if type(self.metric) == list:
            results = []
            for idx, metric in enumerate(self.metric):
                try:
                    if self.result_getter[idx] is getters.get_from_response:
                        if response is None:
                            return -1.0
                        result_state = response
                    else:
                        config = self.evaluator["result"][idx]
                        result_state = self.result_getter[idx](self, config)
                except FileNotFoundError:
                    logger.error("File not found!")
                    if self.metric_conj == 'and':
                        return 0

                expected = self.evaluator["expected"][idx]
                expected_state = self.expected_getter[idx](self, expected) if expected else None

                metric: int = metric(result_state, expected_state,
                                     **self.metric_options[idx]) if expected_state is not None \
                    else metric(result_state, **self.metric_options[idx])

                if self.metric_conj == 'and' and float(metric) == 0.0:
                    return 0
                elif self.metric_conj == 'or' and float(metric) == 1.0:
                    return 1
                else:
                    results.append(metric)
            return sum(results) / len(results) if self.metric_conj == 'and' else max(results)
        else:
            try:
                if self.result_getter is getters.get_from_response:
                    if response is None:
                        return -1.0
                    result_state = response
                else:
                    result_state = self.result_getter(self, self.evaluator["result"])
            except FileNotFoundError:
                logger.error("File not found!")
                return 0

            expected_state = self.expected_getter(self, self.evaluator["expected"]) if "expected" in self.evaluator \
                else None

            metric: float = self.metric(result_state, expected_state,
                                        **self.metric_options) if expected_state is not None \
                else self.metric(result_state, **self.metric_options)

        return metric

    def adversary_evaluate(self):
        """
        Evaluate whether the task is successfully completed.
        """

        if self.adversary_metric is None:
            return -1.0

        if type(self.adversary_metric) == list:
            results = []
            for idx, metric in enumerate(self.adversary_metric):
                try:
                    config = self.adversary_evaluator["result"][idx]
                    result_state = self.adversary_result_getter[idx](self, config)
                except FileNotFoundError:
                    logger.error("File not found!")
                    if self.adversary_metric_conj == 'and':
                        return 0

                expected = self.adversary_evaluator["expected"][idx]
                expected_state = self.adversary_expected_getter[idx](self, expected) if expected else None

                metric: int = metric(result_state, expected_state,
                                     **self.adversary_metric_options[idx]) if expected_state is not None \
                    else metric(result_state, **self.adversary_metric_options[idx])

                if self.adversary_metric_conj == 'and' and float(metric) == 0.0:
                    return 0
                elif self.adversary_metric_conj == 'or' and float(metric) == 1.0:
                    return 1
                else:
                    results.append(metric)
            return sum(results) / len(results) if self.adversary_metric_conj == 'and' else max(results)
        else:
            try:
                result_state = self.adversary_result_getter(self, self.adversary_evaluator["result"])
            except FileNotFoundError:
                logger.error("File not found!")
                return 0

            expected_state = self.adversary_expected_getter(self, self.adversary_evaluator["expected"]) if "expected" in self.adversary_evaluator \
                else None

            metric: float = self.adversary_metric(result_state, expected_state,
                                        **self.adversary_metric_options) if expected_state is not None \
                else self.adversary_metric(result_state, **self.adversary_metric_options)

        return metric


    def render(self, mode='rgb_array'):
        if mode == 'rgb_array':
            return self.controller.get_screenshot()
        else:
            raise ValueError('Unsupported render mode: {}'.format(mode))
