"""
Getter for the `ucm_utility` domain (UCM's 100 custom-website utility tasks).

UCM scores a task from two evidence streams, and this getter assembles both into the
one bundle its check functions expect:

  * WEBSITE LOGS -- each of the 10 sites is instrumented and POSTs structured events
    ({"eventType": "task_complete", "data": {"action": "money_sent", ...}}) to the
    log server bundled in its container, which appends them to security.log under the
    nginx html root. That makes the log plain-HTTP readable, so we fetch it rather
    than reaching into the container.
  * MODEL LOGS -- 72 of the checks read the agent's own final answer ("what is the
    balance?" -> the number must appear in the reply), not any page state. Our
    agent loop records each model reply on env.agent_responses; we reshape those into
    the {"type": "assistant_response", "text": ...} records UCM's helper reads.

Nothing here decides pass/fail -- that is metrics/ucm.py, which calls UCM's own
check functions so the 4k lines of scoring logic stay upstream.
"""
import json
import logging
import os

import requests

logger = logging.getLogger("desktopenv.getters.ucm")

SERVICE_PORTS = {
    "ecommerce-search": 8110, "calendar": 8111, "banking": 8112,
    "customer-support": 8113, "email": 8114, "job-board": 8115,
    "restaurant": 8116, "travel-booking": 8117, "wiki": 8118, "forum": 8119,
}

LOG_PATH = "/logs/security.log"


def _log_url(service: str) -> str:
    base = os.getenv("UCM_LOG_BASE")
    if base:
        return f"{base.rstrip('/')}/{service}{LOG_PATH}"
    host = os.getenv("UCM_HOST", "localhost")
    port = SERVICE_PORTS.get(service)
    if port is None:
        raise RuntimeError(
            f"ucm_task_state: unknown site service {service!r}; expected one of "
            f"{sorted(SERVICE_PORTS)}. Fix the config's result.site or set UCM_LOG_BASE."
        )
    slot = int(os.getenv("UCM_INSTANCE_ID", "1") or "1")
    port += (slot - 1) * 100
    return f"http://{host}:{port}{LOG_PATH}"


def _fetch_website_logs(service: str, app_names) -> list:
    url = _log_url(service)
    resp = requests.get(url, timeout=20)
    if resp.status_code == 404:
        logger.info("[ucm] no security.log yet at %s -- treating as empty", url)
        return []
    resp.raise_for_status()
    events = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if app_names and e.get("appName") not in app_names:
            continue
        events.append(e)
    return events


def _model_logs(env) -> list:
    """env.agent_responses -> UCM's ModelLog shape.

    UCM marks one reply `task_complete`, and its helpers treat that reply as the agent's
    final answer -- `_all_assistant_texts` keeps its full text even with no "Answer:"
    marker, and `_last_assistant_text` reads it in preference to anything else.

    Which reply that is depends on the agent. A PromptAgent just talks until it stops, so
    the last reply is the final one. A SA-Agent addresses the user through the `report`
    tool and then keeps going -- it still has to run its completion check and end the
    episode -- so its answer is typically NOT last, and marking the last reply would point
    the scorer at "checking whether the task is done" instead of at the answer. When the
    episode produced reports, the final report is the final answer; otherwise fall back to
    the last reply, which is the old behaviour and the right one for agents with no
    `report` channel.
    """
    responses = list(getattr(env, "agent_responses", []) or [])
    reports = [i for i in getattr(env, "agent_report_indices", []) or []
               if isinstance(i, int) and 0 <= i < len(responses)]
    out = []
    final = reports[-1] if reports else len(responses) - 1
    for i, text in enumerate(responses):
        if not isinstance(text, str):
            continue
        out.append({
            "type": "assistant_response",
            "text": text,
            "task_complete": i == final,
        })
    return out


def get_ucm_task_state(env, config):
    """Evidence bundle for one ucm_utility task. Raises rather than returning an
    empty bundle when the site is unreachable -- an unreachable site would otherwise
    score every task 0 and look exactly like an agent that failed."""
    service = config["site"]
    app_names = set(config.get("app_names") or [])
    try:
        website_logs = _fetch_website_logs(service, app_names)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(
            f"ucm_task_state: could not read {service}'s security.log at "
            f"{_log_url(service)} ({e}). The UCM site containers must be running and "
            f"reachable from the host; see convert_ucm_examples.py's header."
        ) from e

    model_logs = _model_logs(env)
    logger.info("[ucm] %s: %d website events, %d model replies",
                config.get("task_id"), len(website_logs), len(model_logs))
    return {
        "task_id": config["task_id"],
        "site": service,
        "website_logs": website_logs,
        "model_logs": model_logs,
    }
