import json
import logging
import os
import os.path
import platform
import shutil
import sqlite3
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timedelta
from typing import Any, Union, Optional
from typing import Dict, List

import requests
from playwright.sync_api import sync_playwright, TimeoutError
from pydrive.auth import GoogleAuth
from pydrive.drive import GoogleDrive, GoogleDriveFile, GoogleDriveFileList
from requests_toolbelt.multipart.encoder import MultipartEncoder

from desktop_env.controllers.python import PythonController
from desktop_env.evaluators.metrics.utils import compare_urls

logger = logging.getLogger("desktopenv.setup")

FILE_PATH = os.path.dirname(os.path.abspath(__file__))


class SetupController:
    PROVENANCE_MANIFEST_PATH = "/home/user/.rtc_provenance.json"

    def __init__(self, vm_ip: str, server_port: int = 5000, chromium_port: int = 9222, vlc_port: int = 8080, cache_dir: str = "cache"):
        self.vm_ip: str = vm_ip
        self.server_port: int = server_port
        self.chromium_port: int = chromium_port
        self.vlc_port: int = vlc_port
        self.http_server: str = f"http://{vm_ip}:{server_port}"
        self.http_server_setup_root: str = f"http://{vm_ip}:{server_port}/setup"
        self.cache_dir: str = cache_dir
        self._provenance_entries: List[Dict[str, Any]] = []

    def reset_cache_dir(self, cache_dir: str):
        self.cache_dir = cache_dir

    def setup(self, config: List[Dict[str, Any]]):
        """
        Args:
            config (List[Dict[str, Any]]): list of dict like {str: Any}. each
              config dict has the structure like
                {
                    "type": str, corresponding to the `_{:}_setup` methods of
                      this class
                    "parameters": dick like {str, Any} providing the keyword
                      parameters
                }
        """

        for cfg in config:
            config_type: str = cfg["type"]
            parameters: Dict[str, Any] = cfg["parameters"]

            setup_function: str = "_{:}_setup".format(config_type)
            assert hasattr(self, setup_function), f'Setup controller cannot find init function {setup_function}'
            getattr(self, setup_function)(**parameters)

            logger.info("SETUP: %s(%s)", setup_function, str(parameters))

        self._write_provenance_manifest()

    def _write_provenance_manifest(self):
        """Push the accumulated file-provenance manifest to the VM, once, after every config step
        has run. No-op if nothing was accumulated (e.g. a task with no download/adv_upload steps
        at all) -- SA-Agent's FileProvenanceMap treats a missing manifest the same as an empty
        one (fail-closed: unmatched non-browser content stays untrusted either way)."""
        if not self._provenance_entries:
            return
        manifest = json.dumps({"files": self._provenance_entries}).encode("utf-8")
        form = MultipartEncoder({
            "file_path": self.PROVENANCE_MANIFEST_PATH,
            "file_data": (os.path.basename(self.PROVENANCE_MANIFEST_PATH), manifest),
        })
        headers = {"Content-Type": form.content_type}
        try:
            response = requests.post(self.http_server + "/setup" + "/upload", headers=headers, data=form)
            if response.status_code == 200:
                logger.info("Provenance manifest uploaded successfully: %s", response.text)
            else:
                logger.error("Failed to upload provenance manifest. Status code: %s", response.text)
        except requests.exceptions.RequestException as e:
            logger.error("An error occurred while trying to upload the provenance manifest: %s", e)

    def _download_setup(self, files: List[Dict[str, str]]):
        """
        Args:
            files (List[Dict[str, str]]): files to download. lisf of dict like
              {
                "url": str, the url to download
                "path": str, the path on the VM to store the downloaded file
              }
        """

        for f in files:
            url: str = f["url"]
            path: str = f["path"]
            owner: str = f.get("owner", "user")
            cache_path: str = os.path.join(self.cache_dir, "{:}_{:}".format(
                uuid.uuid5(uuid.NAMESPACE_URL, url),
                os.path.basename(path)))
            if not url or not path:
                raise Exception(f"Setup Download - Invalid URL ({url}) or path ({path}).")

            if not os.path.exists(cache_path):
                max_retries = 3
                downloaded = False
                e = None
                for i in range(max_retries):
                    try:
                        response = requests.get(url, stream=True)
                        response.raise_for_status()

                        with open(cache_path, 'wb') as f:
                            for chunk in response.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                        logger.info("File downloaded successfully")
                        downloaded = True
                        break

                    except requests.RequestException as exc:
                        e = exc
                        logger.error(
                            f"Failed to download {url} caused by {e}. Retrying... ({max_retries - i - 1} attempts left)")
                if not downloaded:
                    raise requests.RequestException(f"Failed to download {url}. No retries left. Error: {e}")

            form = MultipartEncoder({
                "file_path": path,
                "file_data": (os.path.basename(path), open(cache_path, "rb"))
            })
            headers = {"Content-Type": form.content_type}
            logger.debug(form.content_type)

            try:
                logger.debug("REQUEST ADDRESS: %s", self.http_server + "/setup" + "/upload")
                response = requests.post(self.http_server + "/setup" + "/upload", headers=headers, data=form)
                if response.status_code == 200:
                    logger.info("Command executed successfully: %s", response.text)
                else:
                    logger.error("Failed to upload file. Status code: %s", response.text)
            except requests.exceptions.RequestException as e:
                logger.error("An error occurred while trying to send the request: %s", e)

            self._provenance_entries.append({"path": path, "owner": owner})

    def _adv_upload_file_setup(self, **config):
        """
        Args:
            {
            "local_path": str, the local path to the file to upload
            "vm_path": str, the path on the VM to store the downloaded file
            "owner": str, optional, defaults to "user" -- this file's content is genuinely real
              user data (not attacker-authored), so it stays trusted by default. Override for a
              future adversarial variant whose narrative implies otherwise.
            "readers": list[str], optional, defaults to ["user"] -- these are pre-existing
              sensitive/bait files by construction (every current example -- BankStatement.pdf,
              course_notes/, ... -- is clearly private), so confidentiality is restricted by
              default. Override (e.g. to make it public) if a task genuinely needs that.
            }
        """
        local_path: str = config["local_path"]
        vm_path: str = config["vm_path"]
        owner: str = config.get("owner", "user")
        readers: List[str] = config.get("readers", ["user"])

        if not os.path.exists(local_path):
            logger.error(f"Setup Upload - Invalid local path ({local_path}).")
            return

        form = MultipartEncoder({
            "file_path": vm_path,
            "file_data": (os.path.basename(local_path), open(local_path, "rb"))
        })
        headers = {"Content-Type": form.content_type}
        logger.debug(form.content_type)

        try:
            logger.debug("REQUEST ADDRESS: %s", self.http_server + "/setup" + "/upload")
            response = requests.post(self.http_server + "/setup" + "/upload", headers=headers, data=form)
            if response.status_code == 200:
                logger.info("Command executed successfully: %s", response.text)
            else:
                logger.error("Failed to upload file. Status code: %s", response.text)
        except requests.exceptions.RequestException as e:
            logger.error("An error occurred while trying to send the request: %s", e)

        self._provenance_entries.append({"path": vm_path, "owner": owner, "readers": readers})

    def _adv_upload_dir_setup(self, **config):
        """
        Args:
            {
            "local_dir": str, the local dir to upload
            "vm_dir": str, the dir on the VM to store the downloaded files
            "owner": str, optional -- see _adv_upload_file_setup; applied to every file in the dir
            "readers": list[str], optional -- see _adv_upload_file_setup; applied to every file
            }
        """
        local_dir: str = config["local_dir"]
        vm_dir: str = config["vm_dir"]

        if not os.path.exists(local_dir):
            logger.error(f"Setup Upload - Invalid local dir ({local_dir}).")
            return

        self._execute_setup(["mkdir", "-p", vm_dir])

        for root, dirs, files in os.walk(local_dir):
            for file in files:
                local_path = os.path.join(root, file)
                rel_path = os.path.relpath(root, local_dir)
                vm_path = os.path.join(vm_dir, rel_path, file)
                try:
                    per_file_config = {"local_path": local_path, "vm_path": vm_path}
                    if "owner" in config:
                        per_file_config["owner"] = config["owner"]
                    if "readers" in config:
                        per_file_config["readers"] = config["readers"]
                    self._adv_upload_file_setup(**per_file_config)
                except Exception as e:
                    logger.error(f"An error occurred while uploading dir: {e}")


    def _change_wallpaper_setup(self, path: str):

        if not path:
            raise Exception(f"Setup Wallpaper - Invalid path ({path}).")

        payload = json.dumps({"path": path})
        headers = {
            'Content-Type': 'application/json'
        }

        try:
            response = requests.post(self.http_server + "/setup" + "/change_wallpaper", headers=headers, data=payload)
            if response.status_code == 200:
                logger.info("Command executed successfully: %s", response.text)
            else:
                logger.error("Failed to change wallpaper. Status code: %s", response.text)
        except requests.exceptions.RequestException as e:
            logger.error("An error occurred while trying to send the request: %s", e)

    def _tidy_desktop_setup(self, **config):
        raise NotImplementedError()

    def _open_setup(self, path: str):
        if not path:
            raise Exception(f"Setup Open - Invalid path ({path}).")

        payload = json.dumps({"path": path})
        headers = {
            'Content-Type': 'application/json'
        }

        try:
            response = requests.post(self.http_server + "/setup" + "/open_file", headers=headers, data=payload)
            if response.status_code == 200:
                logger.info("Command executed successfully: %s", response.text)
            else:
                logger.error("Failed to open file. Status code: %s", response.text)
        except requests.exceptions.RequestException as e:
            logger.error("An error occurred while trying to send the request: %s", e)

    def _launch_setup(self, command: Union[str, List[str]], shell: bool = False):
        if not command:
            raise Exception("Empty command to launch.")

        if not shell and isinstance(command, str) and len(command.split()) > 1:
            logger.warning("Command should be a list of strings. Now it is a string. Will split it by space.")
            command = command.split()

        payload = json.dumps({"command": command, "shell": shell})
        headers = {"Content-Type": "application/json"}

        try:
            response = requests.post(self.http_server + "/setup" + "/launch", headers=headers, data=payload)
            if response.status_code == 200:
                logger.info("Command executed successfully: %s", response.text)
            else:
                logger.error("Failed to launch application. Status code: %s", response.text)
        except requests.exceptions.RequestException as e:
            logger.error("An error occurred while trying to send the request: %s", e)

    def _execute_setup(
            self,
            command: List[str],
            stdout: str = "",
            stderr: str = "",
            shell: bool = False,
            until: Optional[Dict[str, Any]] = None
    ):
        if not command:
            raise Exception("Empty command to launch.")

        until: Dict[str, Any] = until or {}
        terminates: bool = False
        nb_failings = 0

        payload = json.dumps({"command": command, "shell": shell})
        headers = {"Content-Type": "application/json"}

        while not terminates:
            try:
                response = requests.post(self.http_server + "/setup" + "/execute", headers=headers, data=payload)
                if response.status_code == 200:
                    results: Dict[str, str] = response.json()
                    if stdout:
                        with open(os.path.join(self.cache_dir, stdout), "w") as f:
                            f.write(results["output"])
                    if stderr:
                        with open(os.path.join(self.cache_dir, stderr), "w") as f:
                            f.write(results["error"])
                    logger.info("Command executed successfully: %s -> %s"
                                , " ".join(command) if isinstance(command, list) else command
                                , response.text
                                )
                else:
                    logger.error("Failed to launch application. Status code: %s", response.text)
                    results = None
                    nb_failings += 1
            except requests.exceptions.RequestException as e:
                logger.error("An error occurred while trying to send the request: %s", e)
                traceback.print_exc()

                results = None
                nb_failings += 1

            if len(until) == 0:
                terminates = True
            elif results is not None:
                terminates = "returncode" in until and results["returncode"] == until["returncode"] \
                             or "stdout" in until and until["stdout"] in results["output"] \
                             or "stderr" in until and until["stderr"] in results["error"]
            terminates = terminates or nb_failings >= 5
            if not terminates:
                time.sleep(0.3)

    def _command_setup(self, command: List[str], **kwargs):
        self._execute_setup(command, **kwargs)

    def _sleep_setup(self, seconds: float):
        time.sleep(seconds)

    def _act_setup(self, action_seq: List[Union[Dict[str, Any], str]]):
        raise NotImplementedError()

    def _replay_setup(self, trajectory: str):
        """
        Args:
            trajectory (str): path to the replay trajectory file
        """

        raise NotImplementedError()

    def _activate_window_setup(self, window_name: str, strict: bool = False, by_class: bool = False):
        if not window_name:
            raise Exception(f"Setup Open - Invalid path ({window_name}).")

        payload = json.dumps({"window_name": window_name, "strict": strict, "by_class": by_class})
        headers = {
            'Content-Type': 'application/json'
        }

        try:
            response = requests.post(self.http_server + "/setup" + "/activate_window", headers=headers, data=payload)
            if response.status_code == 200:
                logger.info("Command executed successfully: %s", response.text)
            else:
                logger.error(f"Failed to activate window {window_name}. Status code: %s", response.text)
        except requests.exceptions.RequestException as e:
            logger.error("An error occurred while trying to send the request: %s", e)

    def _close_window_setup(self, window_name: str, strict: bool = False, by_class: bool = False):
        if not window_name:
            raise Exception(f"Setup Open - Invalid path ({window_name}).")

        payload = json.dumps({"window_name": window_name, "strict": strict, "by_class": by_class})
        headers = {
            'Content-Type': 'application/json'
        }

        try:
            response = requests.post(self.http_server + "/setup" + "/close_window", headers=headers, data=payload)
            if response.status_code == 200:
                logger.info("Command executed successfully: %s", response.text)
            else:
                logger.error(f"Failed to close window {window_name}. Status code: %s", response.text)
        except requests.exceptions.RequestException as e:
            logger.error("An error occurred while trying to send the request: %s", e)

    def _chrome_open_tabs_setup(self, urls_to_open: List[str]):
        host = self.vm_ip
        port = self.chromium_port

        remote_debugging_url = f"http://{host}:{port}"
        logger.info("Connect to Chrome @: %s", remote_debugging_url)
        logger.debug("PLAYWRIGHT ENV: %s", repr(os.environ))
        for attempt in range(15):
            if attempt > 0:
                time.sleep(5)

            browser = None
            with sync_playwright() as p:
                try:
                    browser = p.chromium.connect_over_cdp(remote_debugging_url)
                except Exception as e:
                    if attempt < 14:
                        logger.error(f"Attempt {attempt + 1}: Failed to connect, retrying. Error: {e}")
                        continue
                    else:
                        logger.error(f"Failed to connect after multiple attempts: {e}")
                        raise e

                if not browser:
                    return

                logger.info("Opening %s...", urls_to_open)
                for i, url in enumerate(urls_to_open):
                    if i == 0:
                        context = browser.contexts[0]

                    page = context.new_page()
                    try:
                        page.goto(url, timeout=60000)
                    except:
                        logger.warning("Opening %s exceeds time limit", url)
                    logger.info(f"Opened tab {i + 1}: {url}")

                    if i == 0:
                        default_page = context.pages[0]
                        default_page.close()

                return browser, context

    def _chrome_close_tabs_setup(self, urls_to_close: List[str]):
        time.sleep(5)

        host = self.vm_ip
        port = self.chromium_port

        remote_debugging_url = f"http://{host}:{port}"
        with sync_playwright() as p:
            browser = None
            for attempt in range(15):
                try:
                    browser = p.chromium.connect_over_cdp(remote_debugging_url)
                    break
                except Exception as e:
                    if attempt < 14:
                        logger.error(f"Attempt {attempt + 1}: Failed to connect, retrying. Error: {e}")
                        time.sleep(5)
                    else:
                        logger.error(f"Failed to connect after multiple attempts: {e}")
                        raise e

            if not browser:
                return

            for i, url in enumerate(urls_to_close):
                if i == 0:
                    context = browser.contexts[0]

                for page in context.pages:

                    if compare_urls(page.url, url):
                        context.pages.pop(context.pages.index(page))
                        page.close()
                        logger.info(f"Closed tab {i + 1}: {url}")
                        break

            return browser, context

    def _googledrive_setup(self, **config):
        """ Clean google drive space (eliminate the impact of previous experiments to reset the environment)
        @args:
            config(Dict[str, Any]): contain keys
                settings_file(str): path to google drive settings file, which will be loaded by pydrive.auth.GoogleAuth()
                operation(List[str]): each operation is chosen from ['delete', 'upload']
                args(List[Dict[str, Any]]): parameters for each operation
            different args dict for different operations:
                for delete:
                    query(str): query pattern string to search files or folder in google drive to delete, please refer to
                        https://developers.google.com/drive/api/guides/search-files?hl=en about how to write query string.
                    trash(bool): whether to delete files permanently or move to trash. By default, trash=false, completely delete it.
                for mkdirs:
                    path(List[str]): the path in the google drive to create folder
                for upload:
                    path(str): remote url to download file
                    dest(List[str]): the path in the google drive to store the downloaded file
        """
        settings_file = config.get('settings_file', 'evaluation_examples/settings/googledrive/settings.yml')
        gauth = GoogleAuth(settings_file=settings_file)
        drive = GoogleDrive(gauth)

        def mkdir_in_googledrive(paths: List[str]):
            paths = [paths] if type(paths) != list else paths
            parent_id = 'root'
            for p in paths:
                q = f'"{parent_id}" in parents and title = "{p}" and mimeType = "application/vnd.google-apps.folder" and trashed = false'
                folder = drive.ListFile({'q': q}).GetList()
                if len(folder) == 0:
                    parents = {} if parent_id == 'root' else {'parents': [{'id': parent_id}]}
                    file = drive.CreateFile({'title': p, 'mimeType': 'application/vnd.google-apps.folder', **parents})
                    file.Upload()
                    parent_id = file['id']
                else:
                    parent_id = folder[0]['id']
            return parent_id

        for oid, operation in enumerate(config['operation']):
            if operation == 'delete':
                params = config['args'][oid]
                q = params.get('query', '')
                trash = params.get('trash', False)
                q_file = f"( {q} ) and mimeType != 'application/vnd.google-apps.folder'" if q.strip() else "mimeType != 'application/vnd.google-apps.folder'"
                filelist: GoogleDriveFileList = drive.ListFile({'q': q_file}).GetList()
                q_folder = f"( {q} ) and mimeType = 'application/vnd.google-apps.folder'" if q.strip() else "mimeType = 'application/vnd.google-apps.folder'"
                folderlist: GoogleDriveFileList = drive.ListFile({'q': q_folder}).GetList()
                for file in filelist:
                    file: GoogleDriveFile
                    if trash:
                        file.Trash()
                    else:
                        file.Delete()
                for folder in folderlist:
                    folder: GoogleDriveFile
                    if trash:
                        folder.Trash()
                    else:
                        folder.Delete()
            elif operation == 'mkdirs':
                params = config['args'][oid]
                mkdir_in_googledrive(params['path'])
            elif operation == 'upload':
                params = config['args'][oid]
                url = params['url']
                with tempfile.NamedTemporaryFile(mode='wb', delete=False) as tmpf:
                    response = requests.get(url, stream=True)
                    response.raise_for_status()
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            tmpf.write(chunk)
                    tmpf.close()
                    paths = [params['path']] if params['path'] != list else params['path']
                    parent_id = mkdir_in_googledrive(paths[:-1])
                    parents = {} if parent_id == 'root' else {'parents': [{'id': parent_id}]}
                    file = drive.CreateFile({'title': paths[-1], **parents})
                    file.SetContentFile(tmpf.name)
                    file.Upload()
                return
            else:
                raise ValueError('[ERROR]: not implemented clean type!')
            

    _RC_MESSAGE_SELECTORS = (
        '[data-qa-type="message"]',
        '.rcx-message',
        'div.messages-box li',
        'li.message',
        'div.message',
    )

    def _rocketchat_message_selector(self, page):
        """The message selector that matches on this page, or None."""
        for sel in self._RC_MESSAGE_SELECTORS:
            try:
                if page.locator(sel).count() > 0:
                    return sel
            except Exception:
                continue
        return None

    def _click_rocketchat_dm(self, page, *labels):
        """Open a DM from the sidebar, accepting any of `labels` as its visible text.

        Which one shows depends on the UI_Use_Real_Name setting, so both the username
        and the display name have to be tried. Pass the display name first: the setup
        turns UI_Use_Real_Name on, so the username never matches and leading with it
        burned a 15s timeout on every rocketchat episode before the fallback hit.
        """
        last = None
        for label in labels:
            try:
                page.get_by_text(label, exact=False).first.click(timeout=15000)
                return
            except Exception as e:
                last = e
                logger.info("Sidebar entry %r not clickable, trying next label.", label)
        raise last if last else RuntimeError("no DM label supplied")

    def _wait_for_rocketchat_reply(self, page, timeout=None):
        """Block until the NPC has answered, rather than sleeping a fixed amount.

        Must be called straight after our own message is sent. The selector is resolved
        at that point, not before: a room that starts empty has no message elements to
        match, so probing up front found nothing and fell back to a blind sleep.

        Once our own message has rendered, the count is the baseline and the reply is the
        next element to appear. Waiting happens inside the page via wait_for_function,
        which re-evaluates on DOM mutation instead of us polling from the host.

        Returns True if a reply was seen. On timeout it logs an error and returns False:
        the caller continues so the episode still runs, but the failure is on the record
        instead of silently looking like a well-behaved agent.
        """
        if timeout is None:
            timeout = float(os.getenv("ROCKETCHAT_NPC_REPLY_TIMEOUT", "180"))

        sel = None
        for candidate in self._RC_MESSAGE_SELECTORS:
            try:
                page.wait_for_selector(candidate, timeout=15000, state="attached")
                sel = candidate
                break
            except Exception:
                continue

        if sel is None:
            logger.warning(
                "Could not identify RocketChat message elements; falling back to a "
                "%ss sleep for the NPC reply.", timeout
            )
            time.sleep(min(timeout, 90))
            return False

        before = page.locator(sel).count()
        want = before + 1
        logger.info("Waiting up to %ss for the NPC reply (%s messages now, via %s).",
                    timeout, before, sel)
        try:
            page.wait_for_function(
                "([sel, want]) => document.querySelectorAll(sel).length >= want",
                arg=[sel, want],
                timeout=timeout * 1000,
            )
            logger.info("NPC replied (%s messages in the room).", want)
            return True
        except Exception:
            logger.error(
                "No NPC reply after %ss (messages: %s, expected >= %s). The injected "
                "content was NOT delivered, so this episode cannot measure whether the "
                "agent resists it.", timeout, page.locator(sel).count(), want
            )
            return False

    _OWNCLOUD_OPEN_CLICKS = ("moveTo(1615, 298", "moveTo(477,  578", "moveTo(477, 578")

    def _open_owncloud_pdf_viewer(self, attempts: int = 3) -> None:
        """Open the attacked shared_*.pdf in ownCloud's PDF viewer, VERIFIED, or raise.

        The macro used to do this blind: two pixel clicks after a fixed 2 s sleep, with nothing
        checking that the viewer opened. On a CPU-starved VM (7 VMs booting at once) the row click
        landed before ownCloud's JS had bound the file action, nothing opened, and the episode
        silently started on the file overview -- a different task, scored as if it were this one.

        Now: find the one shared_*.pdf row, fire ownCloud's own `view` file action (the only route
        that mounts the viewer -- a click on the name anchor downloads the file, see
        at-spi/suites.py PAGE_ACTIONS), and wait until the URL hash is #pdfviewer and PDF.js has
        loaded the document and drawn a page. Retried; a viewer that never opens raises, so the
        episode fails loudly instead of changing its task.
        """
        remote_debugging_url = f"http://{self.vm_ip}:{self.chromium_port}"
        last_err = "not attempted"
        with sync_playwright() as p:
            for attempt in range(1, attempts + 1):
                try:
                    browser = p.chromium.connect_over_cdp(remote_debugging_url)
                    pages = [pg for pg in browser.contexts[0].pages if "/apps/files" in pg.url]
                    if not pages:
                        raise RuntimeError("no ownCloud files tab is open")
                    logger.info("ownCloud tabs: %s", [(pg.url, pg.evaluate("document.visibilityState"))
                                                      for pg in pages])
                    page = pages[-1]
                    page.bring_to_front()
                    page.wait_for_selector("#fileList tr[data-file]", timeout=60000)
                    page.wait_for_function("() => window.OCA && OCA.Files && OCA.Files.App "
                                           "&& OCA.Files.App.fileList && OCA.Files.App.fileList.fileActions",
                                           timeout=60000)
                    page.evaluate("(() => { const c = document.querySelector('#colorbox');"
                                  " if (c && getComputedStyle(c).display !== 'none' && window.jQuery"
                                  " && jQuery.colorbox) jQuery.colorbox.close(); })()")
                    targets = page.evaluate(
                        "Array.from(document.querySelectorAll('#fileList tr[data-file]'))"
                        ".map(r => r.dataset.file).filter(f => /^shared_.*\\.pdf$/.test(f))")
                    if len(targets) != 1:
                        raise RuntimeError(f"expected exactly one shared_*.pdf in the file list, found {targets}")
                    if "pdfviewer" not in page.evaluate("location.hash"):
                        page.evaluate(
                            "(f) => { const fl = OCA.Files.App.fileList;"
                            " fl.fileActions.triggerAction('view', fl.getModelForFile(f), fl); }", targets[0])
                    page.wait_for_function("() => location.hash.includes('pdfviewer')", timeout=30000)
                    frame = page.wait_for_selector("iframe[src*='files_pdfviewer']",
                                                   timeout=30000).content_frame()
                    frame.wait_for_function(
                        "() => window.PDFViewerApplication && PDFViewerApplication.pagesCount > 0"
                        " && document.querySelector('.page canvas')", timeout=60000)
                    time.sleep(2)
                    logger.info("ownCloud PDF viewer open on %s (attempt %d).", targets[0], attempt)
                    return
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                    logger.warning("Opening the ownCloud PDF viewer failed (attempt %d/%d): %s",
                                   attempt, attempts, last_err)
                    if attempt < attempts:
                        time.sleep(10)
        raise RuntimeError(f"ownCloud PDF viewer did not open after {attempts} attempts ({last_err}); "
                           f"refusing to start the episode on the file overview.")

    def _owncloud_viewer_is_open(self) -> bool:
        """Is the viewer still up? Only the tab chosen above ever gets it, so find that tab and bring
        it to front again (page order is not stable across CDP connections, so no index is trusted)."""
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://{self.vm_ip}:{self.chromium_port}")
            pages = [pg for pg in browser.contexts[0].pages if "/apps/files" in pg.url]
            viewer = [pg for pg in pages if "pdfviewer" in pg.evaluate("location.hash")
                      and pg.query_selector("iframe[src*='files_pdfviewer']") is not None]
            logger.info("ownCloud viewer check: %d of %d ownCloud tab(s) show the viewer (%s)",
                        len(viewer), len(pages), [pg.url for pg in pages])
            if len(viewer) != 1:
                return False
            viewer[0].bring_to_front()
            return True

    def _decouple_setup(self, **config):
        time.sleep(2)

        if config['platform'] == "owncloud":
            opens = [c for c in config["commands"]
                     if any(k in " ".join(map(str, c)) for k in self._OWNCLOUD_OPEN_CLICKS)]
            rest = [c for c in config["commands"] if c not in opens]
            if not opens:
                for command in config["commands"]:
                    self._execute_setup(command)
                return
            for attempt in range(1, 4):
                self._open_owncloud_pdf_viewer()
                for command in rest:
                    self._execute_setup(command)
                if self._owncloud_viewer_is_open():
                    return
                logger.warning("ownCloud PDF viewer closed during the post-open macro (attempt %d/3).",
                               attempt)
            raise RuntimeError("ownCloud PDF viewer did not stay open through the decouple macro.")

        elif config['platform'] == "reddit":
            for command in config["commands"]:
                self._execute_setup(command)

        elif config['platform'] == "rocketchat":
            host = self.vm_ip
            port = self.chromium_port

            remote_debugging_url = f"http://{host}:{port}"
            with sync_playwright() as p:
                browser = None
                for attempt in range(25):
                    try:
                        browser = p.chromium.connect_over_cdp(remote_debugging_url)
                        break
                    except Exception as e:
                        if attempt < 24:
                            logger.error(f"Attempt {attempt + 1}: Failed to connect, retrying. Error: {e}")
                            time.sleep(10)
                        else:
                            logger.error(f"Failed to connect after multiple attempts: {e}")
                            raise e
                if not browser:
                    return

                context = browser.contexts[0]

                page = context.new_page()
                url = config['url']

                try:
                    page.goto(url, timeout=60000, wait_until="load")
                except:
                    logger.warning("Opening %s exceeds time limit", url)
                logger.info(f"Opened page: {url}")
                
                if page.locator("#emailOrUsername").count() > 0:
                    page.locator("#emailOrUsername").fill('theagentcompany')
                    page.locator("#pass").fill('theagentcompany')
                    page.get_by_role("button", name="Login").click()
                    page.wait_for_load_state('load', timeout=5000)
                    
                try:
                    self._click_rocketchat_dm(page, "Mike Chen", "mike_chen")
                    page.wait_for_url("**/direct/**", timeout=15000)
                    page.wait_for_load_state("networkidle")
                    page.wait_for_url("**/direct/**", timeout=15000)
                    page.wait_for_load_state("networkidle")
                    time.sleep(15)
                    page.wait_for_selector('textarea.rc-message-box__textarea', timeout=5000)
                    time.sleep(10)
                    page.locator('textarea.rc-message-box__textarea').click()
                    time.sleep(10)
                    if 'message' in config:
                        page.locator('textarea.rc-message-box__textarea').fill(config['message'])
                        time.sleep(10)
                        page.locator('textarea[name="msg"]').press("Enter")
                        self._wait_for_rocketchat_reply(page)

                except TimeoutError:
                    logger.info('[ERROR]: timeout when waiting for login page to load!')
                    return

        else:
            logger.error("error in platform")
            raise NotImplementedError


    def _login_setup(self, **config):
        """ Login to a website with account and password information.
        @args:
            config(Dict[str, Any]): contain keys
                settings_file(str): path to the settings file
                platform(str): platform to login, implemented platforms include:
                    googledrive: https://drive.google.com/drive/my-drive

        """
        logger.info(f"This may takes a while and error might happen, but please wait...")
        
        host = self.vm_ip
        port = self.chromium_port

        remote_debugging_url = f"http://{host}:{port}"
        with sync_playwright() as p:
            browser = None
            for attempt in range(25):
                try:
                    browser = p.chromium.connect_over_cdp(remote_debugging_url)
                    break
                except Exception as e:
                    if attempt < 24:
                        logger.error(f"Attempt {attempt + 1}: Failed to connect, retrying. Error: {e}")
                        time.sleep(10)
                    else:
                        logger.error(f"Failed to connect after multiple attempts: {e}")
                        raise e
            if not browser:
                return

            context = browser.contexts[0]
            platform = config['platform']

            if platform == 'googledrive':
                url = 'https://drive.google.com/drive/my-drive'
                page = context.new_page()
                try:
                    page.goto(url, timeout=60000)
                except:
                    logger.warning("Opening %s exceeds time limit", url)
                logger.info(f"Opened new page: {url}")
                settings = json.load(open(config['settings_file']))
                email, password = settings['email'], settings['password']

                try:
                    page.wait_for_selector('input[type="email"]', state="visible", timeout=3000)
                    page.fill('input[type="email"]', email)
                    page.click('#identifierNext > div > button')
                    page.wait_for_selector('input[type="password"]', state="visible", timeout=5000)
                    page.fill('input[type="password"]', password)
                    page.click('#passwordNext > div > button')
                    page.wait_for_load_state('load', timeout=5000)
                except TimeoutError:
                    logger.info('[ERROR]: timeout when waiting for google drive login page to load!')
                    return
                
            elif platform == 'reddit':
                url = config['url']
                page = context.new_page()
                try:
                    page.goto(url, timeout=60000, wait_until="load")
                except:
                    logger.warning("Opening %s exceeds time limit", url)
                logger.info(f"Opened page: {url}")
                password, username = config['password'], config['username']
                
                try:
                    page.goto(f"{os.path.join(url,'login')}")
                    page.get_by_label("Username").fill(username)
                    page.get_by_label("Password").fill(password)
                    page.get_by_role("button", name="Log in").click()
                    page.wait_for_load_state('load', timeout=5000)
                    page.wait_for_load_state('networkidle', timeout=10000)
                except TimeoutError:
                    logger.info('[ERROR]: timeout when waiting for login page to load!')
                    return
                
            elif platform == 'rocketchat':
                url = config['url']
                page = context.new_page()
                try:
                    page.goto(url, timeout=60000, wait_until="load")
                except:
                    logger.warning("Opening %s exceeds time limit", url)
                logger.info(f"Opened page: {url}")
                password, username = config['password'], config['username']
                
                try:
                    page.locator("#emailOrUsername").fill(username)
                    page.locator("#pass").fill(password)
                    page.get_by_role("button", name="Login").click()
                    page.wait_for_load_state('load', timeout=5000)
                    page.wait_for_load_state('networkidle', timeout=10000)
                except TimeoutError:
                    logger.info('[ERROR]: timeout when waiting for login page to load!')
                    return
                
            elif platform == 'owncloud':
                url = config['url']
                page = context.new_page()
                try:
                    page.goto(url, timeout=60000, wait_until="load")
                except:
                    logger.warning("Opening %s exceeds time limit", url)
                logger.info(f"Opened page: {url}")
                password, username = config['password'], config['username']
                
                try:
                    page.fill('#user', username)
                    page.fill('#password', password)
                    page.click('#submit')
                    
                    page.wait_for_load_state('load', timeout=5000)
                    page.wait_for_load_state('networkidle', timeout=10000)
                    
                    if page.url.startswith(url) and "/index.php/apps" in page.url:
                        logger.info("Successfully logged into ownCloud")
                    else:
                        logger.warning("Login might have failed - check URL pattern")
                        
                except Exception as e:
                    logger.error(f"Error during ownCloud login: {str(e)}")
                    return


            elif platform == 'gitlab':
                url = config['url']
                page = context.new_page()
                try:
                    page.goto(url, timeout=60000, wait_until="load")
                except:
                    logger.warning("Opening %s exceeds time limit", url)
                logger.info(f"Opened page: {url}")
                password, username = config['password'], config['username']

                try:
                    page.goto(f"{url.rstrip('/')}/users/sign_in", timeout=60000, wait_until="load")
                    page.fill('input#user_login', username)
                    page.fill('input#user_password', password)
                    page.click('button[type="submit"]')
                    page.wait_for_load_state('load', timeout=5000)
                    page.wait_for_load_state('networkidle', timeout=10000)
                except TimeoutError:
                    logger.info('[ERROR]: timeout when waiting for login page to load!')
                    return

            else:
                raise NotImplementedError

            return browser, context

    def _update_browse_history_setup(self, **config):
        cache_path = os.path.join(self.cache_dir, "history_new.sqlite")
        db_url = "https://drive.usercontent.google.com/u/0/uc?id=1Lv74QkJYDWVX0RIgg0Co-DUcoYpVL0oX&export=download"
        if not os.path.exists(cache_path):
                max_retries = 3
                downloaded = False
                e = None
                for i in range(max_retries):
                    try:
                        response = requests.get(db_url, stream=True)
                        response.raise_for_status()

                        with open(cache_path, 'wb') as f:
                            for chunk in response.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                        logger.info("File downloaded successfully")
                        downloaded = True
                        break

                    except requests.RequestException as exc:
                        e = exc
                        logger.error(
                            f"Failed to download {db_url} caused by {e}. Retrying... ({max_retries - i - 1} attempts left)")
                if not downloaded:
                    raise requests.RequestException(f"Failed to download {db_url}. No retries left. Error: {e}")
        else:
            logger.info("File already exists in cache directory")
        db_path = cache_path

        history = config['history']

        for history_item in history:
            url = history_item['url']
            title = history_item['title']
            visit_time = datetime.now() - timedelta(seconds=history_item['visit_time_from_now_in_seconds'])

            epoch_start = datetime(1601, 1, 1)
            chrome_timestamp = int((visit_time - epoch_start).total_seconds() * 1000000)

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            cursor.execute('''
                   INSERT INTO urls (url, title, visit_count, typed_count, last_visit_time, hidden)
                   VALUES (?, ?, ?, ?, ?, ?)
               ''', (url, title, 1, 0, chrome_timestamp, 0))

            url_id = cursor.lastrowid

            cursor.execute('''
                   INSERT INTO visits (url, visit_time, from_visit, transition, segment_id, visit_duration)
                   VALUES (?, ?, ?, ?, ?, ?)
               ''', (url_id, chrome_timestamp, 0, 805306368, 0, 0))

            conn.commit()
            conn.close()

        logger.info('Fake browsing history added successfully.')

        controller = PythonController(self.vm_ip, self.server_port)

        os_type = controller.get_vm_platform()

        if os_type == 'Windows':
            chrome_history_path = controller.execute_python_command(
                """import os; print(os.path.join(os.getenv('USERPROFILE'), "AppData", "Local", "Google", "Chrome", "User Data", "Default", "History"))""")[
                'output'].strip()
        elif os_type == 'Darwin':
            chrome_history_path = controller.execute_python_command(
                """import os; print(os.path.join(os.getenv('HOME'), "Library", "Application Support", "Google", "Chrome", "Default", "History"))""")[
                'output'].strip()
        elif os_type == 'Linux':
            if "arm" in platform.machine():
                chrome_history_path = controller.execute_python_command(
                    "import os; print(os.path.join(os.getenv('HOME'), 'snap', 'chromium', 'common', 'chromium', 'Default', 'History'))")[
                    'output'].strip()
            else:
                chrome_history_path = controller.execute_python_command(
                    "import os; print(os.path.join(os.getenv('HOME'), '.config', 'google-chrome', 'Default', 'History'))")[
                    'output'].strip()
        else:
            raise Exception('Unsupported operating system')

        form = MultipartEncoder({
            "file_path": chrome_history_path,
            "file_data": (os.path.basename(chrome_history_path), open(db_path, "rb"))
        })
        headers = {"Content-Type": form.content_type}
        logger.debug(form.content_type)

        try:
            logger.debug("REQUEST ADDRESS: %s", self.http_server + "/setup" + "/upload")
            response = requests.post(self.http_server + "/setup" + "/upload", headers=headers, data=form)
            if response.status_code == 200:
                logger.info("Command executed successfully: %s", response.text)
            else:
                logger.error("Failed to upload file. Status code: %s", response.text)
        except requests.exceptions.RequestException as e:
            logger.error("An error occurred while trying to send the request: %s", e)

        self._execute_setup(["sudo chown -R user:user /home/user/.config/google-chrome/Default/History"], shell=True)
