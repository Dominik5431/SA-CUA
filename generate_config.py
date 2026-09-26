import argparse
import os
import json
import re
import copy

from collections.abc import Mapping

def deep_update(dst: dict, src: Mapping) -> None:
    """
    Recursively merge keys from `src` into `dst` in-place.
    If the same key exists and both values are dicts, merge them deeper;
    otherwise overwrite `dst[key]` with `src[key]`.
    """
    for k, v in src.items():
        if isinstance(v, Mapping) and isinstance(dst.get(k), Mapping):
            deep_update(dst[k], v)
        else:
            dst[k] = v

def sub_url(url: str, target_url: str = ""):
    url =  re.sub(r"__REDDIT__", r"TO_BE_REPLACED_URL" + ":9999", url)
    url =  re.sub(r"__ROCKETCHAT__", r"TO_BE_REPLACED_URL" + ":3000", url)
    url =  re.sub(r"__OWNCLOUD__", r"TO_BE_REPLACED_URL" + ":8092", url)
    url =  re.sub(r"__GITLAB__", r"TO_BE_REPLACED_URL" + ":8023", url)
    url =  re.sub(r"__TARGET_URL__", target_url, url)
    return url

def replace_in_dict(data, target_url: str = ""):
    if isinstance(data, dict):
        return {key: replace_in_dict(val, target_url) for key, val in data.items()}
    elif isinstance(data, list):
        return [replace_in_dict(item, target_url) for item in data]
    elif isinstance(data, tuple):
        return tuple(replace_in_dict(item, target_url) for item in data)
    elif isinstance(data, str):
        return sub_url(data, target_url)
    else:
        return data


PLATFORM_CONTACT_DIRECTORY = {
    "rocketchat": [
        "Alex Turner <@alex_turner>",
        "Chen Xinyi <@chen_xinyi>",
        "David Wong <@david_wong>",
        "Emily Zhou <@emily_zhou>",
        "Emma Lewis <@emma_lewis>",
        "Huang Jie <@huang_jie>",
        "Jessica Chen <@jessica_chen>",
        "Jessica Lee <@jessica_lee>",
        "Li Ming <@li_ming>",
        "Liu Qiang <@liu_qiang>",
        "Mark Johnson <@mark_johnson>",
        "Mike Chen <@mike_chen>",
        "Priya Sharma <@priya_sharma>",
        "Sarah Johnson <@sarah_johnson>",
        "Sophia Rodriguez <@sophia_rodriguez>",
        "Wang Fang <@wang_fang>",
        "Zhang Wei <@zhang_wei>",
    ],
    "gitlab": [
        "Alice Kim <@alicekim>",
        "Byte Blaze <@byteblaze>",
    ],
}


def domain_of(benign_task):
    """
    The examples/ subdirectory -- and matching test_all_<domain>.json index --
    a task belongs to.

    Defaults to the platform, so the original RTC goals keep landing in
    examples/reddit, examples/owncloud and examples/rocketchat.  A goal that
    carries an explicit "domain" is routed to its own suite directory instead
    (the WASP ports go to wasp_reddit / wasp_gitlab).  "platform" is untouched
    by this and keeps driving the runtime env setup in desktop_env.py, so a
    suite directory costs nothing at run time: run.py only ever uses the domain
    as a path segment, for the config path and for the result path.
    """
    return benign_task.get("domain") or benign_task["platform"]


def create_config(benign_task_raw, adversary_task_raw, output_path):
    indexes = {}

    def index_for(domain):
        if domain not in indexes:
            try:
                with open(f"{output_path}/test_all_{domain}.json", "r") as f:
                    indexes[domain] = json.load(f)
            except Exception:
                indexes[domain] = {}
            indexes[domain].setdefault(domain, [])
        return indexes[domain]

    for benign_task in benign_task_raw:
        domain = domain_of(benign_task)
        config_dict = index_for(domain)

        for benign_task_type in ["specific", "loose"]:
            if benign_task_type not in benign_task["instruction"]:
                continue
            config = {}
            config["platform"] = benign_task["platform"]
            config["meta_data"] = {
                "benign_task_type": benign_task_type
            }
            config["instruction"] = benign_task["instruction"][benign_task_type]["instruction"]

            _contacts = PLATFORM_CONTACT_DIRECTORY.get(benign_task["platform"])
            if _contacts:
                config["trusted_contacts"] = list(_contacts)

            config["tags"] = {
                "permission_check": True,
                "safety_check": True
            }

            config["config"] = [
                {
                    "type": "launch",
                    "parameters": {
                        "command": [
                        "google-chrome",
                        "--remote-debugging-port=1337",
                        "--user-data-dir=/tmp/chrome_user_data",
                        "--disable-session-crashed-bubble",
                        "--disable-save-password-bubble",
                        "--password-store=basic",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-infobars",
                        "--disable-component-update",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--start-maximized"
                        ]
                    }
                },
                {
                    "type": "launch",
                    "parameters": {
                        "command": [
                        "socat",
                        "tcp-listen:9222,fork",
                        "tcp:localhost:1337"
                        ]
                    }
                }
            ]

            if benign_task["platform"] == "reddit":
                config["config"].append(
                    {
                        "type": "login",
                        "parameters": {
                            "url": sub_url("__REDDIT__"),
                            "username": "MarvelsGrantMan136",
                            "password": "test1234",
                            "platform": "reddit"
                        }
                    }
                )
            
            elif benign_task["platform"] == "rocketchat":
                config["config"] = benign_task["pre_launch"] + config["config"]
                config["config"].append(
                    {
                        "type": "login",
                        "parameters": {
                            "url": sub_url("__ROCKETCHAT__"),
                            "username": "theagentcompany",
                            "password": "theagentcompany",
                            "platform": "rocketchat"
                        }
                    }
                )
            
            elif benign_task["platform"] == "owncloud":
                config["config"] = benign_task["pre_launch"] + config["config"]
                config["config"].append(
                    {
                        "type": "login",
                        "parameters": {
                            "url": sub_url("__OWNCLOUD__"),
                            "username": "theagentcompany",
                            "password": "theagentcompany",
                            "platform": "owncloud"
                        }
                    }
                )

            elif benign_task["platform"] == "gitlab":
                config["config"].append(
                    {
                        "type": "login",
                        "parameters": {
                            "url": sub_url("__GITLAB__"),
                            "username": "byteblaze",
                            "password": "hello1234",
                            "platform": "gitlab"
                        }
                    }
                )

            else:
                print(f"[ERROR] {benign_task['platform']} is not supported.")

            config["config"].append(
                {
                    "type": "chrome_open_tabs",
                    "parameters": {
                        "urls_to_open": [sub_url(benign_task["target_url"])]
                    }
                }
            )
            config["config"].append(
                {
                    "type": "activate_window",
                    "parameters": {
                        "window_name": "Google Chrome"
                    }
                }
            )
            
            if "post_opentabs" in benign_task:
                for c in benign_task["post_opentabs"]:
                    config["config"].append(c)
            
            config["evaluator"] = replace_in_dict(benign_task["evaluator"])


            for adversary_task in adversary_task_raw:
                config_for_adv = copy.deepcopy(config)

                if "extra_preprocessing" in adversary_task:
                    for c in adversary_task["extra_preprocessing"]:
                        config_for_adv["config"].append(c)

                for injection_contents in adversary_task["injection_contents"]:
                    config_for_adv["meta_data"]["adversary_injection_type"] = injection_contents["type"]
                    
                    if benign_task["platform"] == "rocketchat":
                        deep_update(
                            injection_contents["action"]["parameters"],
                            benign_task["meta_injection"]["parameters"],
                        )
                    else:
                        injection_contents["action"]["parameters"].update(benign_task["meta_injection"]["parameters"])
                        
                    injection_contents["action"].update({"ip":"TO_BE_REPLACED_IP"})
                    config_for_adv["adversary_config"] = replace_in_dict([injection_contents["action"]], sub_url(benign_task["target_url"]))
                    config_for_adv["adversary_evaluator"] = replace_in_dict(adversary_task["evaluator"], sub_url(benign_task["target_url"]))
                    if "_wasp_source" in adversary_task:
                        config_for_adv["wasp_source"] = replace_in_dict(
                            adversary_task["_wasp_source"], sub_url(benign_task["target_url"])
                        )

                    config_name = "_".join([benign_task["id"], benign_task_type, adversary_task["description"], injection_contents["type"]])
                    config_dict[domain].append(config_name)

                    dir_path = os.path.join(f'{output_path}/examples', domain)
                    os.makedirs(dir_path, exist_ok=True)

                    config_for_adv["id"] = config_name
                    
                    config_for_adv["adv_id"] = adversary_task["id"]
                    with open(os.path.join(dir_path, config_name + ".json"), "w") as f:
                        json.dump(config_for_adv, f, indent=4)


    for domain, index in indexes.items():
        with open(f"{output_path}/test_all_{domain}.json", "w") as f:
            json.dump(index, f, indent=4)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("--benign_task_raw", type=str, default="goals/benign/benign_task.raw_own_rocketchat_install_nodejs.json")
    parser.add_argument("--adversary_task_raw", type=str, default="goals/adv/adversary_task.raw_own_rocketchat_install_nodejs.json")
    parser.add_argument("--output_path", type=str, default="evaluation_examples_own_rocketchat")

    args = parser.parse_args()

    with open(args.benign_task_raw, "r") as f:
        benign_task_raw = json.load(f)

    with open(args.adversary_task_raw, "r") as f:
        adversary_task_raw = json.load(f)

    create_config(benign_task_raw, adversary_task_raw, args.output_path)
