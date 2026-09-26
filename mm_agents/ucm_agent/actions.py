"""
Translate ucm_agent's `computer_use` tool-call actions into the pyautogui
code strings `DesktopEnv.step()` executes when `action_space="pyautogui"`
(see desktop_env/desktop_env.py:610-640 and
desktop_env/controllers/python.py's `execute_python_command`, which runs
the string as `python -c "import pyautogui; ...; {command}"` inside the VM).

The action shape mirrors the source UCM repo's `computer_use` tool
(src/agent/agent_claude_client.py's `_computer_use_tool_def`) — a single
custom Anthropic tool, not the native `computer` beta tool — kept
deliberately simple (click/double_click/scroll/type/keypress/wait/move/
drag/screenshot) plus goto/back/forward, which ucm_agent routes through
BrowserGuard/CDP instead of GUI automation (far more reliable than driving
the address bar with pyautogui — see UCM_AGENT_INTEGRATION_PLAN.md §5).
"""

from typing import Any, Dict, List, Optional

CDP_ACTIONS = {"goto", "back", "forward"}


def _escape_for_triple_quoted(text: str) -> str:
    """Make `text` safe to embed inside a Python triple-double-quoted
    string literal (used for pyautogui.typewrite(\"\"\"...\"\"\"))."""
    return text.replace('\\', '\\\\').replace('"""', '\\"\\"\\"')


def build_pyautogui_command(action: Dict[str, Any]) -> str:
    """Build a pyautogui code string for one `computer_use` action.

    Raises ValueError for goto/back/forward (those never reach here — the
    loop routes them to BrowserGuard before calling this) and for unknown
    action types.
    """
    action_type = action.get("type")
    if action_type in CDP_ACTIONS:
        raise ValueError(f"'{action_type}' is a CDP-routed action, not a pyautogui command")

    lines: List[str] = []

    if action_type == "click":
        x, y = action.get("x"), action.get("y")
        button = (action.get("button") or "left").lower()
        button_map = {"left": "click", "right": "rightClick", "middle": "middleClick"}
        fn = button_map.get(button, "click")
        if x is not None and y is not None:
            lines.append(f"pyautogui.{fn}({int(x)}, {int(y)})")
        else:
            lines.append(f"pyautogui.{fn}()")

    elif action_type == "double_click":
        x, y = action.get("x"), action.get("y")
        if x is not None and y is not None:
            lines.append(f"pyautogui.doubleClick({int(x)}, {int(y)})")
        else:
            lines.append("pyautogui.doubleClick()")

    elif action_type == "move":
        x, y = action.get("x"), action.get("y")
        lines.append(f"pyautogui.moveTo({int(x)}, {int(y)}, duration=0.3)")

    elif action_type == "drag":
        path = action.get("path") or []
        for i, point in enumerate(path):
            px = point.get("x") if isinstance(point, dict) else point[0]
            py = point.get("y") if isinstance(point, dict) else point[1]
            if i == 0:
                lines.append(f"pyautogui.moveTo({int(px)}, {int(py)}, duration=0.2)")
                lines.append("pyautogui.mouseDown()")
            else:
                lines.append(f"pyautogui.moveTo({int(px)}, {int(py)}, duration=0.2)")
        lines.append("pyautogui.mouseUp()")

    elif action_type == "scroll":
        x = action.get("x")
        y = action.get("y")
        scroll_x = int(action.get("scroll_x") or 0)
        scroll_y = int(action.get("scroll_y") or 0)
        if x is not None and y is not None:
            lines.append(f"pyautogui.moveTo({int(x)}, {int(y)})")
        if scroll_y:
            lines.append(f"pyautogui.scroll({scroll_y})")
        if scroll_x:
            lines.append(f"pyautogui.hscroll({scroll_x})")
        if not scroll_x and not scroll_y:
            lines.append("pyautogui.sleep(0.1)")

    elif action_type == "type":
        text = action.get("text", "")
        safe_text = _escape_for_triple_quoted(text)
        lines.append(f'pyautogui.typewrite("""{safe_text}""", interval=0.01)')

    elif action_type == "keypress":
        keys = action.get("keys") or []
        key_conversion = {
            "page_down": "pagedown", "page_up": "pageup",
            "super_l": "win", "super": "command", "escape": "esc",
            "return": "enter",
        }
        norm_keys = [key_conversion.get(k.strip().lower(), k.strip().lower()) for k in keys]
        for k in norm_keys:
            lines.append(f"pyautogui.keyDown('{k}')")
        for k in reversed(norm_keys):
            lines.append(f"pyautogui.keyUp('{k}')")

    elif action_type == "wait":
        ms = action.get("ms", 1000)
        lines.append(f"time.sleep({max(0, ms) / 1000})")

    elif action_type == "screenshot":
        lines.append("pyautogui.sleep(0.1)")

    else:
        raise ValueError(f"Unknown computer_use action type: {action_type!r}")

    return "\n".join(lines) + "\n"


def click_coords(action: Dict[str, Any]) -> Optional[tuple]:
    """(x, y) for actions that have a meaningful click point, else None —
    used only for debug screenshot annotation."""
    if action.get("type") in ("click", "double_click", "move"):
        x, y = action.get("x"), action.get("y")
        if x is not None and y is not None:
            return (x, y)
    return None
