"""Which model the RocketChat NPCs (Sotopia) talk through.

Standard library only: run_one_npc.py imports this from the NPC virtualenv, and run.py
imports it from the harness env to check the configuration before the first task.
"""
import json
import os
import urllib.request


def resolve_npc_model() -> str:
    """Return a Sotopia-compatible model string for the NPC agent.

    Priority:
    1. VLLM_MODEL_FOR_ROCKETCHAT_NPC — uses the running vLLM server via the
       Sotopia custom/<model>@<base_url> path (OpenAI-compatible endpoint).
    2. AZURE_MODEL_FOR_ROCKETCHAT_NPC — original Azure OpenAI path, expected
       format: resource_name/deployment_name/api_version

    The NPC's port is taken from ROCKETCHAT_NPC_VLLM_PORT when set, so the NPC
    can run on a dedicated server (its own model on its own GPU) independent of
    the agent's VLLM_PORT. It falls back to VLLM_PORT when unset, which keeps the
    old behaviour of the NPC sharing the agent's server.
    """
    vllm_model = os.getenv('VLLM_MODEL_FOR_ROCKETCHAT_NPC')
    if vllm_model:
        vllm_port = os.getenv('ROCKETCHAT_NPC_VLLM_PORT') or os.getenv('VLLM_PORT', '8000')
        return f"custom/{vllm_model}@http://localhost:{vllm_port}/v1"

    azure_model = os.getenv('AZURE_MODEL_FOR_ROCKETCHAT_NPC')
    if azure_model:
        if azure_model.startswith("openai/"):
            azure_model = azure_model[7:]
        return f"azure/{azure_model}"

    raise RuntimeError(
        "No NPC model configured. Set either:\n"
        "  VLLM_MODEL_FOR_ROCKETCHAT_NPC=<model>  (uses the running vLLM server)\n"
        "  AZURE_MODEL_FOR_ROCKETCHAT_NPC=<resource>/<deployment>/<version>  (Azure OpenAI)\n"
        "in the environment or in the repo's .env (run.py loads it; an export still wins)."
    )


def check_npc_model(timeout: float = 10.0) -> str:
    """Resolve the NPC model and, for the vLLM path, confirm that server serves it.

    Raises RuntimeError otherwise. Each NPC is launched per task, inside
    prepare_injection, so a missing model used to surface only there: once per
    rocketchat task, after the VM boot and the platform reset, and never as a stop.
    """
    model = resolve_npc_model()
    if not model.startswith("custom/"):
        return model
    name, _, base_url = model[len("custom/"):].rpartition("@")
    try:
        with urllib.request.urlopen(f"{base_url}/models", timeout=timeout) as resp:
            served = [m.get("id") for m in json.load(resp).get("data", [])]
    except Exception as e:
        raise RuntimeError(
            f"NPC model server {base_url} is unreachable ({e}); the NPC could not answer."
        ) from e
    if name not in served:
        raise RuntimeError(
            f"NPC model {name!r} is not served at {base_url} (it serves {served})."
        )
    return model
