"""Human-in-the-loop agent.

Instead of asking a model for the next action, ``HumanAgent.predict`` opens a
small pygame window that shows the current screenshot and records the human's
mouse/keyboard input.  The recorded events are translated into pyautogui code
strings (the same representation the other agents return) and handed back to the
regular ``run_predict_step_loop`` driver, so screenshots and ``traj.jsonl`` are
saved in exactly the same way as for a model-driven agent.
"""

from __future__ import annotations

import json
import logging
import os
from io import BytesIO
from typing import Dict, List, Tuple

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame

logger = logging.getLogger("desktopenv.human_agent")


def _py_string(text: str) -> str:
    """Return a Python string literal for pyautogui code generation."""
    return json.dumps("" if text is None else str(text), ensure_ascii=False)


class HumanAgent:
    """An agent whose ``predict`` blocks on human input from a pygame window."""

    def __init__(
        self,
        action_space: str = "pyautogui",
        observation_type: str = "screenshot",
        screen_width: int = 1920,
        screen_height: int = 1080,
    ):
        if action_space != "pyautogui":
            raise ValueError("HumanAgent only supports the pyautogui action space")
        self.action_space = action_space
        self.observation_type = observation_type
        self.screen_width = screen_width
        self.screen_height = screen_height

        self.cua = False

        self.step_idx = 0
        self.example_result_dir = None
        self.thoughts: List[str] = []
        self.actions: List[str] = []
        self.observations: List[Dict] = []

    def reset(self, tags=None, _logger=None):
        global logger
        logger = _logger if _logger is not None else logging.getLogger("desktopenv.human_agent")
        self.thoughts = []
        self.actions = []
        self.observations = []
        self.step_idx = 0

    def predict(self, instruction: str, obs: Dict) -> Tuple[str, List[str], bool]:
        """Show the current observation to the human and return their actions."""
        screenshot = obs.get("screenshot")
        if not screenshot:
            logger.warning("HumanAgent received no screenshot; returning WAIT")
            return "No screenshot available", ["WAIT"], False

        recorder = _HumanActionRecorder(
            screenshot_bytes=screenshot,
            instruction=instruction,
            step_idx=self.step_idx,
        )
        actions = recorder.run()

        response = "\n".join(actions) if actions else "No action"
        self.thoughts.append(response)
        self.actions.append(actions)
        self.observations.append(obs)
        return response, actions, False


class _HumanActionRecorder:
    """A single-use pygame window that records human input as pyautogui code."""

    MARGIN = 10
    HEADER_H = 104
    LOG_H = 56
    BUTTON_H = 30
    SLACK = 60

    _MODIFIER_KEYS = {
        pygame.K_LCTRL, pygame.K_RCTRL,
        pygame.K_LSHIFT, pygame.K_RSHIFT,
        pygame.K_LALT, pygame.K_RALT,
        pygame.K_LMETA, pygame.K_RMETA,
        pygame.K_LSUPER, pygame.K_RSUPER,
    }

    _SPECIAL_KEYS = {
        pygame.K_RETURN: "enter",
        pygame.K_KP_ENTER: "enter",
        pygame.K_ESCAPE: "esc",
        pygame.K_BACKSPACE: "backspace",
        pygame.K_TAB: "tab",
        pygame.K_DELETE: "delete",
        pygame.K_HOME: "home",
        pygame.K_END: "end",
        pygame.K_PAGEUP: "pageup",
        pygame.K_PAGEDOWN: "pagedown",
        pygame.K_UP: "up",
        pygame.K_DOWN: "down",
        pygame.K_LEFT: "left",
        pygame.K_RIGHT: "right",
        pygame.K_INSERT: "insert",
        pygame.K_PRINTSCREEN: "printscreen",
        pygame.K_SCROLLOCK: "scrolllock",
        pygame.K_PAUSE: "pause",
    }
    for _i in range(1, 25):
        _key = getattr(pygame, f"K_F{_i}", None)
        if _key is not None:
            _SPECIAL_KEYS[_key] = f"f{_i}"

    _KEY_NAME_MAP = {
        "return": "enter",
        "escape": "esc",
        "page up": "pageup",
        "page down": "pagedown",
    }

    def __init__(self, screenshot_bytes: bytes, instruction: str, step_idx: int):
        from PIL import Image

        self.image = Image.open(BytesIO(screenshot_bytes)).convert("RGB")
        self.instruction = instruction
        self.step_idx = step_idx

        self.screen = None
        self.font = None
        self.small_font = None
        self.full_surface = None
        self.display_surface = None

        self.actions: List[str] = []
        self.typed: List[str] = []
        self._last_click = None

        self.scale = 1.0
        self.display_w = self.image.width
        self.display_h = self.image.height
        self.win_w = 0
        self.win_h = 0
        self.img_x = self.MARGIN
        self.img_y = self.HEADER_H
        self.buttons = []
        self._exit = False

    def run(self) -> List[str]:
        pygame.init()
        self._fit_display_size()
        self.screen = pygame.display.set_mode((self.win_w, self.win_h))
        pygame.display.set_caption(f"Human agent - step {self.step_idx}")
        self.font = pygame.font.Font(None, 20)
        self.small_font = pygame.font.Font(None, 16)

        self._prepare_surfaces()
        self._layout_buttons()

        clock = pygame.time.Clock()
        running = True
        while running:
            for event in pygame.event.get():
                self._handle_event(event)
                if self._exit:
                    running = False
                    break
            self._draw()
            clock.tick(30)

        pygame.quit()
        return list(self.actions)

    def _fit_display_size(self):
        info = pygame.display.Info()
        screen_w = max(400, int(info.current_w * 0.92))
        screen_h = max(300, int(info.current_h * 0.90))

        max_w = max(400, screen_w - 2 * self.MARGIN - self.SLACK)
        max_h = max(
            300,
            screen_h - self.HEADER_H - self.LOG_H - 2 * self.MARGIN - self.SLACK,
        )

        orig_w, orig_h = self.image.size
        self.scale = min(max_w / orig_w, max_h / orig_h, 1.0)
        self.display_w = max(1, int(orig_w * self.scale))
        self.display_h = max(1, int(orig_h * self.scale))
        self.win_w = self.display_w + 2 * self.MARGIN
        self.win_h = self.HEADER_H + self.display_h + self.LOG_H + 2 * self.MARGIN

    def _prepare_surfaces(self):
        self.full_surface = pygame.image.fromstring(
            self.image.tobytes(), self.image.size, "RGB"
        )
        if (self.display_w, self.display_h) != self.image.size:
            self.display_surface = pygame.transform.smoothscale(
                self.full_surface, (self.display_w, self.display_h)
            )
        else:
            self.display_surface = self.full_surface

    def _layout_buttons(self):
        labels = [
            ("Execute", self._submit),
            ("WAIT", self._wait),
            ("Clear", self._clear),
            ("DONE", self._done),
            ("FAIL", self._fail),
        ]
        gap = 8
        n = len(labels)
        avail = self.win_w - 2 * self.MARGIN
        button_w = min(92, max(1, (avail - (n - 1) * gap) // n))
        total_w = n * button_w + (n - 1) * gap
        start_x = (self.win_w - total_w) // 2
        y = self.HEADER_H - self.BUTTON_H - 8

        self.buttons = []
        for i, (label, command) in enumerate(labels):
            rect = pygame.Rect(start_x + i * (button_w + gap), y, button_w, self.BUTTON_H)
            self.buttons.append((rect, label, command))

    def _wrap_text(self, text, font, max_width):
        words = str(text).split()
        lines = []
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if font.size(candidate)[0] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                while font.size(word)[0] > max_width and len(word) > 1:
                    cut = len(word)
                    while cut > 1 and font.size(word[:cut])[0] > max_width:
                        cut -= 1
                    lines.append(word[:cut])
                    word = word[cut:]
                current = word
        if current:
            lines.append(current)
        return lines or [""]

    def _draw_text(self, text, x, y, color=(230, 230, 230), font=None):
        font = font or self.font
        surface = font.render(text, True, color)
        self.screen.blit(surface, (x, y))

    def _draw(self):
        bg = (30, 30, 30)
        panel = (45, 45, 45)
        button = (70, 70, 70)
        button_text = (230, 230, 230)
        accent = (120, 180, 255)

        self.screen.fill(bg)

        self._draw_text(f"Step {self.step_idx}", self.MARGIN, 4, color=accent)
        all_lines = self._wrap_text(
            self.instruction, self.font, self.win_w - 2 * self.MARGIN
        )
        if len(all_lines) > 2:
            all_lines = all_lines[:2]
            all_lines[1] = all_lines[1][: max(0, (self.win_w - 40) // 8)] + "..."
        for i, line in enumerate(all_lines):
            self._draw_text(line, self.MARGIN, 20 + i * 18)

        for rect, label, _command in self.buttons:
            pygame.draw.rect(self.screen, button, rect, border_radius=4)
            text = self.font.render(label, True, button_text)
            self.screen.blit(
                text,
                (
                    rect.x + (rect.width - text.get_width()) // 2,
                    rect.y + (rect.height - text.get_height()) // 2,
                ),
            )

        self.screen.blit(self.display_surface, (self.img_x, self.img_y))

        footer_y = self.img_y + self.display_h + self.MARGIN
        pygame.draw.rect(
            self.screen, panel, (0, footer_y, self.win_w, self.LOG_H)
        )

        typing_preview = "".join(self.typed)
        if typing_preview:
            preview = f"typing: {typing_preview!r}"
            if self.small_font.size(preview)[0] > self.win_w - 2 * self.MARGIN:
                preview = preview[: (self.win_w - 40) // 8] + "..."
            self._draw_text(preview, self.MARGIN, footer_y + 2, color=accent, font=self.small_font)

        for i, action in enumerate(self.actions[-2:]):
            num = len(self.actions) - len(self.actions[-2:]) + i + 1
            line = f"{num}. {action}"
            if self.small_font.size(line)[0] > self.win_w - 2 * self.MARGIN:
                line = line[: (self.win_w - 40) // 8] + "..."
            self._draw_text(
                line, self.MARGIN, footer_y + 20 + i * 16,
                color=(200, 200, 200), font=self.small_font,
            )

        pygame.display.flip()

    def _to_screen(self, window_x, window_y):
        x = int(round((window_x - self.img_x) / self.scale))
        y = int(round((window_y - self.img_y) / self.scale))
        x = max(0, min(self.image.width - 1, x))
        y = max(0, min(self.image.height - 1, y))
        return x, y

    def _handle_event(self, event):
        if event.type == pygame.QUIT:
            self._submit()
        elif event.type == pygame.KEYDOWN:
            self._handle_keydown(event)
        elif event.type == pygame.MOUSEBUTTONDOWN:
            self._handle_mousedown(event)
        elif event.type == pygame.MOUSEWHEEL:
            self._handle_wheel(event)

    def _handle_mousedown(self, event):
        if event.button == 1:
            for rect, _label, command in self.buttons:
                if rect.collidepoint(event.pos):
                    command()
                    return
            self._handle_left_click(event.pos[0], event.pos[1])
        elif event.button == 3:
            self._flush_typed()
            x, y = self._to_screen(event.pos[0], event.pos[1])
            self._add(f"pyautogui.rightClick({x}, {y})")
        elif event.button == 2:
            self._flush_typed()
            x, y = self._to_screen(event.pos[0], event.pos[1])
            self._add(f"pyautogui.middleClick({x}, {y})")

    def _handle_left_click(self, window_x, window_y):
        x, y = self._to_screen(window_x, window_y)
        now = pygame.time.get_ticks()

        if self._last_click is not None:
            lx, ly, lt = self._last_click
            if (
                now - lt < 400
                and abs(lx - x) <= 8
                and abs(ly - y) <= 8
                and self.actions
                and self.actions[-1] == f"pyautogui.click({lx}, {ly})"
            ):
                self.actions[-1] = f"pyautogui.doubleClick({x}, {y})"
                self._last_click = None
                return

        self._flush_typed()
        self._add(f"pyautogui.click({x}, {y})")
        self._last_click = (x, y, now)

    def _handle_wheel(self, event):
        self._flush_typed()
        clicks = 1 if event.y > 0 else -1
        self._add(f"pyautogui.scroll({clicks})")

    def _mods_in_order(self, mod):
        mods = []
        if mod & pygame.KMOD_CTRL:
            mods.append("ctrl")
        if mod & pygame.KMOD_ALT:
            mods.append("alt")
        if mod & pygame.KMOD_SHIFT:
            mods.append("shift")
        if mod & pygame.KMOD_GUI:
            mods.append("win")
        return mods

    def _map_key_name(self, key):
        name = pygame.key.name(key)
        if not name:
            return None
        return self._KEY_NAME_MAP.get(name, name)

    def _handle_keydown(self, event):
        key = event.key

        if key in self._MODIFIER_KEYS:
            return

        mods = self._mods_in_order(event.mod)

        if mods:
            if mods == ["shift"] and event.unicode and event.unicode.isprintable():
                self.typed.append(event.unicode)
                return
            self._flush_typed()
            mapped = self._map_key_name(key)
            if mapped:
                keys = mods + [mapped]
                self._add(
                    f"pyautogui.hotkey({', '.join(_py_string(k) for k in keys)})"
                )
            return

        if key in self._SPECIAL_KEYS:
            self._flush_typed()
            self._add(f"pyautogui.press({_py_string(self._SPECIAL_KEYS[key])})")
            return

        if event.unicode and event.unicode.isprintable():
            self.typed.append(event.unicode)
            return

    def _flush_typed(self):
        if self.typed:
            text = "".join(self.typed)
            self.typed = []
            self._add(f"pyautogui.typewrite({_py_string(text)})")

    def _add(self, code):
        self.actions.append(code)

    def _submit(self):
        self._flush_typed()
        if not self.actions:
            self.actions.append("WAIT")
        self._exit = True

    def _wait(self):
        self._flush_typed()
        self.actions.append("WAIT")
        self._exit = True

    def _done(self):
        self._flush_typed()
        self.actions.append("DONE")
        self._exit = True

    def _fail(self):
        self.actions = ["FAIL"]
        self._exit = True

    def _clear(self):
        self._last_click = None
        self.typed = []
        self.actions = []
