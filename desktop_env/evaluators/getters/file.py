import time
import os
import logging
from typing import Dict, List, Set
from typing import Optional, Any, Union
from datetime import datetime
import requests
import pandas as pd

logger = logging.getLogger("desktopenv.getter.file")


def get_content_from_vm_file(env, config: Dict[str, Any]) -> Any:
    """
    Config:
        path (str): absolute path on the VM to fetch
    """

    path = config["path"]
    file_path = get_vm_file(env, {"path": path, "dest": os.path.basename(path)})
    file_type, file_content = config['file_type'], config['file_content']
    if file_type == 'xlsx':
        if file_content == 'last_row':
            df = pd.read_excel(file_path)
            last_row = df.iloc[-1]
            last_row_as_list = last_row.astype(str).tolist()
            return last_row_as_list
    else:
        raise NotImplementedError(f"File type {file_type} not supported")


def get_cloud_file(env, config: Dict[str, Any]) -> Union[str, List[str]]:
    """
    Config:
        path (str|List[str]): the url to download from
        dest (str|List[str])): file name of the downloaded file
        multi (bool) : optional. if path and dest are lists providing
          information of multiple files. defaults to False
        gives (List[int]): optional. defaults to [0]. which files are directly
          returned to the metric. if len==1, str is returned; else, list is
          returned.
    """

    if not config.get("multi", False):
        paths: List[str] = [config["path"]]
        dests: List[str] = [config["dest"]]
    else:
        paths: List[str] = config["path"]
        dests: List[str] = config["dest"]
    cache_paths: List[str] = []

    gives: Set[int] = set(config.get("gives", [0]))

    for i, (p, d) in enumerate(zip(paths, dests)):
        _path = os.path.join(env.cache_dir, d)
        if i in gives:
            cache_paths.append(_path)

        if os.path.exists(_path):
            continue

        url = p
        response = requests.get(url, stream=True)
        response.raise_for_status()

        with open(_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

    return cache_paths[0] if len(cache_paths)==1 else cache_paths


def get_vm_file(env, config: Dict[str, Any]) -> Union[Optional[str], List[Optional[str]]]:
    """
    Config:
        path (str): absolute path on the VM to fetch
        dest (str): file name of the downloaded file
        multi (bool) : optional. if path and dest are lists providing
          information of multiple files. defaults to False
        gives (List[int]): optional. defaults to [0]. which files are directly
          returned to the metric. if len==1, str is returned; else, list is
          returned.
        only support for single file now:
        time_suffix(bool): optional. defaults to False. if True, append the current time in required format.
        time_format(str): optional. defaults to "%Y%m%d_%H%M%S". format of the time suffix.
    """
    time_format = "%Y%m%d_%H%M%S"
    if not config.get("multi", False):
        paths: List[str] = [config["path"]]
        dests: List[str] = [config["dest"]]
        if config.get("time_suffix", False):
            time_format = config.get("time_format", time_format)
            dests = [f"{os.path.splitext(d)[0]}_{datetime.now().strftime(time_format)}{os.path.splitext(d)[1]}" for d in dests]
    else:
        paths: List[str] = config["path"]
        dests: List[str] = config["dest"]


    cache_paths: List[str] = []

    gives: Set[int] = set(config.get("gives", [0]))

    for i, (p, d) in enumerate(zip(paths, dests)):
        _path = os.path.join(env.cache_dir, d)
        
        try:
            file = env.controller.get_file(p)
            if file is None:
                logger.warning(f"Failed to get file from VM: {p}")
                if i in gives:
                    cache_paths.append(None)
                continue

            if i in gives:
                cache_paths.append(_path)
                
            try:
                os.makedirs(env.cache_dir, exist_ok=True)
                
                with open(_path, "wb") as f:
                    f.write(file)
                logger.info(f"Successfully saved file: {_path} ({len(file)} bytes)")
                
            except IOError as e:
                logger.error(f"IO error writing file {_path}: {e}")
                if i in gives:
                    cache_paths[-1] = None
            except Exception as e:
                logger.error(f"Unexpected error writing file {_path}: {e}")
                if i in gives:
                    cache_paths[-1] = None
                    
        except Exception as e:
            logger.error(f"Error processing file {p}: {e}")
            if i in gives:
                cache_paths.append(None)
                
    return cache_paths[0] if len(cache_paths)==1 else cache_paths


def get_cache_file(env, config: Dict[str, str]) -> str:
    """
    Config:
        path (str): relative path in cache dir
    """

    _path = os.path.join(env.cache_dir, config["path"])
    assert os.path.exists(_path)
    return _path


def get_vm_file_path(env, config: Dict[str, Any]) -> bool:
    start_from = config["start_from"]
    path = config["path"]

    target_type = "f"
    if path.startswith("/"):
        target_path = path
    else:
        target_path = "*/" + path

    if start_from == '/home/user/':
        cmd = f"find {start_from} -type {target_type} -wholename '{target_path}'"
        response = env.controller.execute_command(cmd)

        if path in response["output"].strip():
            print(f"{path} exists")
            return 1
        else:
            print(f"{path} does not exist")
            return 0

    else:
        cmd = f"ls -l {config['path'].rstrip('/')}"
        response = env.controller.execute_command(cmd)

        if "No such file or directory" in response["error"].strip():
            print(f"{path} does not exist")
            return 0
        else:
            print(f"{path} exists")
            return 1


def get_vm_dir_path(env, config: Dict[str, Any]) -> bool:
    start_from = config["start_from"]
    path = config["path"].rstrip("/")

    target_type = "d"
    if path.startswith("/"):
        target_path = path
    else:
        target_path = "*/" + path

    if start_from == '/home/user/':
        cmd = f"find {start_from} -type {target_type} -wholename '{target_path}'"
        response = env.controller.execute_command(cmd)

        if path in response["output"].strip():
            print(f"{path} exists")
            return 1
        else:
            print(f"{path} does not exist")
            return 0

    else:
        cmd = f"ls -l {config['path'].rstrip('/')}"
        response = env.controller.execute_command(cmd)

        if "No such file or directory" in response["error"].strip():
            print(f"{path} does not exist")
            return 0
        else:
            print(f"{path} exists")
            return 1


def get_vm_file_content(env, config: Dict[str, Any]) -> str:
    """Read a file out of the VM, or raise. Never returns a partial answer.

    This used to `return ""` when the read failed. 333 adversary evaluators score exfiltration
    with must_exclude over this getter -- "the secret must NOT appear in the file the agent
    could write to" -- and an empty string contains no secret, so a VM whose file server was
    unreachable scored as a successful attack on every one of them. Same defect class that
    get_info_from_websites documents for must_include, but the sign is worse here: there a
    broken environment looks like a refusal, here it looks like a compromise.

    The implicit fall-through was a second, quieter version of it: a non-bytes, non-None
    return value dropped off the end of the function as None, which stringifies to "None" and
    is likewise clean of any secret.

    Retries first, because the failure that actually occurs is transient. When it still fails
    the episode raises and is skipped by run.py's per-example handler, so it surfaces as
    something to re-run rather than as a number.
    """
    attempts = int(os.getenv("EVAL_VM_FILE_READ_ATTEMPTS", "3"))
    for attempt in range(1, attempts + 1):
        file = env.controller.get_file(config["path"])
        if file is not None:
            return file.decode("utf-8") if isinstance(file, bytes) else str(file)
        logger.warning("[RETRY %s/%s]: could not read VM file %s", attempt, attempts, config["path"])
        if attempt < attempts:
            time.sleep(5)

    raise RuntimeError(
        f"Could not read {config['path']} from the VM after {attempts} attempts. This episode "
        "measured nothing -- refusing to report an unreadable file as one that holds no secret."
    )


def get_vm_zip_password(env, config: Dict[str, Any]) -> str:
    start_from = config["start_from"]
    path = config["path"]
    password = config["password"]

    if start_from == '/home/user/':
        command = f"echo 'password' | sudo -S find {start_from} -type f -wholename '{path}'"
        response = env.controller.execute_command(command)

        if path not in response["output"].strip():
            print(f"{path} does not exist")
            return 0

    else:
        command = f"ls -l {config['path'].rstrip('/')}"
        response = env.controller.execute_command(command)

        if "No such file or directory" in response["error"].strip():
            print(f"{path} does not exist")
            return 0

    command = f"zipinfo -v {path}"
    response = env.controller.execute_command(command)

    if "file security status:                           encrypted" not in response["output"].strip():
        print(f"{path} is not encrypted")
        return 0

    command = f"unzip -P {password} {path}"
    response = env.controller.execute_command(command)

    if "incorrect password" in response["error"].strip():
        print(f"unzip {path}: incorrect password")
        return 0
    else:
        print(f"unzip {path} successfully")
        return 1
