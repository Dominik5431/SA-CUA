#!/usr/bin/env python3
"""Recalibrate the decouple initial scroll for redesigned tasks (RTC_fix.md composition).

The decouple step scrolls the page to frame the on-screen instructions before the agent
acts. Two redesigned suites need different amounts than the originals:

  own_reddit_*   -> scroll -8 (user-set): applied to every reddit_fixed decouple scroll
                   (reddit_1/2/3) so the framing is uniform across the reddit posts, with the
                   on-screen instruction comment visible in the initial screenshot.
  own_owncloud_2 -> scroll to the top (+2): the redesigned nmon tutorial is ONE short page,
  own_owncloud_3    but the decouple still scrolled -6/-5 (calibrated for the old six-page
                   htop PDF) and shot past the content, showing only the doc's tail. A small
                   upward scroll lands on the top where the title, install command and the
                   adversarial note are all visible together.

own_owncloud_1 (the kept dataset task) still uses its original long PDF, so its scroll is
left alone.

Rewrites the `pyautogui.scroll(N)` integer inside the decouple commands for the matched
task-id prefixes. Usage:  fix_decouple_scroll.py <examples dir>
Prints the number of configs changed.  Idempotent.
"""
import glob
import json
import os
import re
import sys

SCROLL = {
    "own_reddit_": -8,
    "own_owncloud_2_": 2,
    "own_owncloud_3_": 2,
}

_SCROLL_RE = re.compile(r"pyautogui\.scroll\(\s*-?\d+\s*\)")


def patch(directory):
    changed = 0
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        base = os.path.basename(path)
        want = next((v for k, v in SCROLL.items() if base.startswith(k)), None)
        if want is None:
            continue
        with open(path) as fh:
            config = json.load(fh)
        hit = False
        for step in config.get("config", []):
            if step.get("type") != "decouple":
                continue
            for cmd in step.get("parameters", {}).get("commands", []):
                s = cmd[-1]
                if "pyautogui.scroll(" in s:
                    new = _SCROLL_RE.sub(f"pyautogui.scroll({want})", s)
                    if new != s:
                        cmd[-1] = new
                        hit = True
        if hit:
            with open(path, "w") as fh:
                json.dump(config, fh, indent=4)
                fh.write("\n")
            changed += 1
    return changed


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(patch(sys.argv[1]))
