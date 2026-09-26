"""Import UCM's pipeline modules out of the sibling checkout, without modifying it.

Their pipeline/ modules are scripts that do `from _site_loader import ...` at module level, so
they only import with pipeline/ on sys.path. They are also not a package -- there is no
__init__.py -- so they cannot be imported by dotted name. Both are handled here, once, so no
other module in this package has to know where the checkout is or how it is laid out.

UCM_REPO can point the run at a different checkout. The default is the sibling directory this
corpus was built against.
"""
import importlib.util
import os
import sys

_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))), "untrusted-content-masking")

UCM_REPO = os.environ.get("UCM_REPO", _DEFAULT)
PIPELINE_DIR = os.path.join(UCM_REPO, "src", "automatic_boundary_detection", "pipeline")

_cache = {}


def _load(name: str):
    if name in _cache:
        return _cache[name]
    path = os.path.join(PIPELINE_DIR, f"{name}.py")
    if not os.path.exists(path):
        raise SystemExit(
            f"UCM checkout not found or incomplete: {path} does not exist.\n"
            f"Set UCM_REPO to the untrusted-content-masking checkout (currently {UCM_REPO!r}).")
    if PIPELINE_DIR not in sys.path:
        sys.path.insert(0, PIPELINE_DIR)
    spec = importlib.util.spec_from_file_location(f"ucm_pipeline.{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    _cache[name] = mod
    return mod


def analyze_untrusted_elements():
    """The LLM proposal loop: analyze_with_llm(), generate_config()."""
    return _load("analyze_untrusted_elements")


def analyze_selectors():
    """The evaluator: GET_ELEMENTS_JS, get_elements_with_ids()."""
    return _load("analyze_selectors")


def provenance() -> dict:
    """What was actually imported, for a run's meta file."""
    import subprocess
    try:
        sha = subprocess.run(["git", "-C", UCM_REPO, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip() or None
        dirty = bool(subprocess.run(["git", "-C", UCM_REPO, "status", "--porcelain"],
                                    capture_output=True, text=True, timeout=10).stdout.strip())
    except Exception:
        sha, dirty = None, None
    return {"ucm_repo": UCM_REPO, "ucm_git_sha": sha, "ucm_worktree_dirty": dirty,
            "ucm_llm_model_const": getattr(_cache.get("analyze_untrusted_elements"),
                                           "LLM_MODEL", None)}
