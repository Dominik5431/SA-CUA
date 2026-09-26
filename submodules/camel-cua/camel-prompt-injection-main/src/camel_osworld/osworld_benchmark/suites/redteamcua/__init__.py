from .suite import make_redteamcua_task_suite, make_redteamcua_task_suite_Anthropic
from . import user_tasks as _user_tasks

CLI_TO_SUITE_ID = getattr(_user_tasks, "CLI_TO_SUITE_ID", {})
ADVERSARY_SCORES = getattr(_user_tasks, "ADVERSARY_SCORES", {})

__all__ = [
    "make_redteamcua_task_suite",
    "make_redteamcua_task_suite_Anthropic",
    "CLI_TO_SUITE_ID",
    "ADVERSARY_SCORES",
]
