#!/usr/bin/env python3
"""
collect_atspi_apps.py -- sample real AT-SPI2 accessibility-tree XML from native desktop apps
(apps.py), the non-browser sibling of collect_atspi_trees.py/sites.py. Grew out of an earlier
feasibility spike -- every launch shape in
apps.py already has a confirmed-working result; this script is what turns that into real,
indexed corpus entries (xml/*.xml + screenshots/*.png + manifest.json rows), the same artifacts
collect_atspi_trees.py produces for websites, so both arms of the corpus live in one place with
one manifest.

Boots a single DesktopEnv (same constructor shape as collect_atspi_trees.py),
materializes apps.py's FIXTURES and FIXTURE_COMMANDS into the VM, then launches each apps.py entry
in turn, replays its post_launch actions, and captures its AT-SPI tree + a screenshot.

Three things changed when apps.py was reworked into a deep-per-app corpus:

  * the manifest's `domain` is the entry's `app`, not its `key`, so all seven Thunderbird captures
    share one corroboration domain instead of looking like seven domains of one capture each;
  * `post_launch` action lists are replayed after the window settles, which is how an app gets
    captured in a state other than its start screen (the app-side counterpart of sites.py putting
    filters and dates in the URL);
  * `expect_text` is asserted against the captured tree, and a capture that misses it is written to
    disk but recorded as `status: "content_missing"` rather than `"ok"`. This is not belt-and-braces:
    the previous Thunderbird capture landed on the first-run wizard and entered the ground truth as
    a mail client with zero untrusted rows, because a desktop capture's failure mode is a perfectly
    well-formed tree of the wrong thing.

Indices continue on from wherever the web corpus (collect_atspi_trees.py) left off, auto-detected
from the existing manifest.json's max index, so both arms share one index space and one
manifest.json without colliding -- pass --start_index to override (e.g. to leave room for a web
corpus that hasn't finished a resize yet).

Usage:
  python at-spi/collect_atspi_apps.py                  # all apps.APPS entries
  python at-spi/collect_atspi_apps.py --only thunderbird     # a whole app (all its captures)
  python at-spi/collect_atspi_apps.py --only gedit_notes,vscode_app_py
  python at-spi/collect_atspi_apps.py --category communication,office
  python at-spi/collect_atspi_apps.py --list            # preview the selection + assigned indices
  python at-spi/collect_atspi_apps.py --start_index 300 # force a specific starting index
"""
import argparse
import base64
import datetime
import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desktop_env.desktop_env import DesktopEnv
import manifest_io
from apps import APPS, VM_APPS_DIR, FIXTURES, FIXTURE_COMMANDS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("collect_atspi_apps")

HERE = os.path.dirname(os.path.abspath(__file__))

_NS = {
    "attr": "https://accessibility.ubuntu.example.org/ns/attributes",
    "cp": "https://accessibility.ubuntu.example.org/ns/component",
}


def _q(prefix: str, name: str) -> str:
    return f"{{{_NS[prefix]}}}{name}"


def _role(node) -> str:
    tag = node.tag
    if tag.startswith("{"):
        tag = tag.split("}", 1)[1]
    return tag.replace("-", " ")


def _guess_title(xml_text: str, app_substrings: list) -> "str | None":
    """Best-effort analog of collect_atspi_trees.py's page.title() -- the first named `frame`
    role under this app's own `application` subtree. None (not an error) if the app subtree or
    a titled frame isn't found."""
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return None
    apps = [n for n in root.iter() if _role(n) == "application"]
    target = None
    for n in apps:
        name = (n.get("name") or "").lower()
        if any(s in name for s in app_substrings):
            target = n
            break
    if target is None:
        return None
    for n in target.iter():
        if _role(n) == "frame" and (n.get("name") or "").strip():
            return n.get("name").strip()
    return None


def _write_content_files(env) -> None:
    """Materializes apps.py's FIXTURES (literal files) then FIXTURE_COMMANDS (derived artifacts:
    format conversions, archives, a git history, the Thunderbird mailbox).

    The content itself used to be hard-coded here. It moved into apps.py so that the sample material
    lives beside the entries that open it -- the same split sites.py has, where the file owns what
    the corpus contains and the collector only knows how to fetch it.
    """
    run = env.controller.execute_command

    run(f"rm -rf {VM_APPS_DIR}")
    dirs = sorted({os.path.dirname(rel) for rel in FIXTURES if os.path.dirname(rel)})
    run(f"mkdir -p {VM_APPS_DIR} " + " ".join(f"'{VM_APPS_DIR}/{d}'" for d in dirs))

    for rel, content in FIXTURES.items():
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        run(f"echo {b64} | base64 -d > '{VM_APPS_DIR}/{rel}'")
    logger.info("wrote %d fixture file(s)", len(FIXTURES))

    for cmd in FIXTURE_COMMANDS:
        result = run(f"cd {VM_APPS_DIR} && {cmd}")
        output = (result or {}).get("output", "") if isinstance(result, dict) else result
        logger.info("fixture cmd: %s\n%s", cmd[:90], (output or "").strip()[:600])


def _run_post_launch(env, actions: list, index: int) -> None:
    """Replays an entry's post_launch action list -- the app-side equivalent of sites.py putting
    state in the URL. An app at its start screen is the least informative state it has, and unlike a
    URL there is no way to ask for "the inbox with the third message selected" declaratively.

    Every action is best-effort: a failed keystroke is logged and skipped rather than failing the
    capture, because the capture may still be perfectly good. What must not be best-effort is the
    result -- that is what expect_text checks, and a post_launch that silently did nothing will show
    up there as a missing substring.
    """
    for action in actions or []:
        kind = action["type"]
        try:
            if kind == "wait":
                time.sleep(float(action["seconds"]))
                continue
            if kind == "command":
                env.controller.execute_command(action["command"])
            elif kind == "hotkey":
                keys = ", ".join(repr(k) for k in action["keys"])
                env.controller.execute_python_command(f"pyautogui.hotkey({keys})")
            elif kind == "press":
                keys = ", ".join(repr(k) for k in action["keys"])
                times = int(action.get("times", 1))
                for _ in range(times):
                    env.controller.execute_python_command(f"pyautogui.press([{keys}])")
                    time.sleep(0.4)
            elif kind == "type":
                env.controller.execute_python_command(
                    f"pyautogui.typewrite({action['text']!r}, interval=0.05)")
            elif kind == "click":
                env.controller.execute_python_command(
                    f"pyautogui.click({int(action['x'])}, {int(action['y'])})")
            else:
                logger.warning("[%03d] unknown post_launch action type %r, skipped", index, kind)
                continue
            time.sleep(1.0)
        except Exception:
            logger.exception("[%03d] post_launch action %r failed, continuing", index, action)


def _check_expected(xml_text: str, expected: list) -> str:
    """"" if every expect_text substring is present in the captured tree, else "missing:<first>".

    This exists because of the previous Thunderbird capture. It landed on the first-run account
    wizard, produced a well-formed 76-row tree and a plausible screenshot, was labeled by hand, and
    entered the ground truth as an email client with zero untrusted content. Nothing in the pipeline
    could have noticed: the failure mode of a desktop capture is not an error, it is a real tree of
    the wrong thing -- exactly what detect_bot_walls.py catches on the web side.
    """
    haystack = xml_text.lower()
    for needle in expected or []:
        if needle.lower() not in haystack:
            return f"missing:{needle}"
    return ""


def _detect_start_index(manifest_path: str) -> int:
    """One past the highest index on disk.

    A SNAPSHOT, not a reservation: a capture run started concurrently will claim indices after this
    too, and the two runs would then overwrite each other's files. Pass --start_index explicitly
    whenever another run is in flight -- the app block conventionally sits well clear of the web
    block and its consent variants.
    """
    return manifest_io.max_index(manifest_path) + 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider_name", default="docker")
    ap.add_argument("--snapshot_name", default="init_state")
    ap.add_argument("--screen_width", type=int, default=1920)
    ap.add_argument("--screen_height", type=int, default=1080)
    ap.add_argument("--only", default=None,
                    help="Comma-separated apps.py `key`s or `app`s to (re)capture (default: all)")
    ap.add_argument("--category", default=None,
                    help="Comma-separated apps.py categories to keep, e.g. 'communication,office'")
    ap.add_argument("--start_index", type=int, default=None,
                     help="First manifest index to assign (default: auto, one past manifest.json's current max)")
    ap.add_argument("--out_dir", default=os.path.join(HERE, "xml"))
    ap.add_argument("--screenshot_dir", default=os.path.join(HERE, "screenshots"))
    ap.add_argument("--list", dest="list_only", action="store_true", help="Preview selection + indices, no VM boot")
    args = ap.parse_args()

    manifest_path = os.path.join(HERE, "manifest.json")
    start_index = args.start_index if args.start_index is not None else _detect_start_index(manifest_path)

    indexed = [(start_index + pos, e) for pos, e in enumerate(APPS)]

    if args.only:
        wanted = {k.strip() for k in args.only.split(",") if k.strip()}
        indexed = [(i, e) for i, e in indexed if e["key"] in wanted or e.get("app") in wanted]
        if not indexed:
            ap.error(f"No apps.py entry matched --only={args.only!r}. "
                     f"Known keys: {[e['key'] for e in APPS]}. "
                     f"Known apps: {sorted({e.get('app', e['key']) for e in APPS})}")

    if args.category:
        wanted_cats = {c.strip() for c in args.category.split(",") if c.strip()}
        indexed = [(i, e) for i, e in indexed if e["category"] in wanted_cats]
        if not indexed:
            ap.error(f"No apps.py entry matched --category={args.category!r}. "
                     f"Known: {sorted({e['category'] for e in APPS})}")

    entries = [e for _i, e in indexed]

    if args.list_only:
        for i, e in indexed:
            marks = ("+post" if e.get("post_launch") else "    ") + ("+exp" if e.get("expect_text") else "    ")
            print(f"{i:03d}  {e['category']:16s} {e.get('app', e['key']):18s} {e['key']:32s} "
                  f"settle={e['settle_seconds']:3d}s {marks}  {' '.join(e['argv'])[:80]}")
        return

    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(args.screenshot_dir, exist_ok=True)
    manifest_by_index: dict[int, dict] = {}

    def _write_manifest() -> None:
        manifest_io.merge_rows(manifest_path, manifest_by_index)

    all_kill_patterns = sorted({e["kill_pattern"] for e in APPS})

    logger.info("Booting DesktopEnv (%s)...", args.provider_name)
    env = DesktopEnv(
        provider_name=args.provider_name,
        action_space="pyautogui",
        require_a11y_tree=True,
        screen_size=(args.screen_width, args.screen_height),
        headless=True,
        os_type="Ubuntu",
        snapshot_name=args.snapshot_name,
    )
    try:
        env.prepare_injection()
        logger.info("VM ready (vm_ip=%s). Writing content files...", env.vm_ip)
        _write_content_files(env)

        succeeded = 0
        for pos, (i, e) in enumerate(indexed, start=1):
            key = e["key"]
            filename = f"{i:03d}_{key}.xml"
            screenshot_filename = f"{i:03d}_{key}.png"
            row = {
                "index": i, "category": e["category"], "url": f"app://{key}",
                "domain": e.get("app", key), "app_key": key,
                "filename": filename, "screenshot_filename": screenshot_filename,
                "status": "error", "title": None,
                "xml_bytes": 0, "screenshot_bytes": 0, "error": None,
                "content_check": None,
                "timestamp": datetime.datetime.now().isoformat(),
                "platform": "app", "content_note": e["content_note"],
            }
            try:
                logger.info("[%d/%d] app #%03d: %s (%s)", pos, len(indexed), i, key, e["category"])
                for pat in all_kill_patterns:
                    env.controller.execute_command(f"pkill -f {pat} || true")
                time.sleep(3)

                argv = ["env"] + e["env"] + e["argv"]
                env.setup_controller.setup([{"type": "launch", "parameters": {"command": argv}}])
                time.sleep(e["settle_seconds"])

                if env.dismiss_update_banner():
                    logger.info("[%03d] dismissed a pending 'Software Updates' banner", i)

                if e.get("post_launch"):
                    logger.info("[%03d] replaying %d post_launch action(s)", i, len(e["post_launch"]))
                    _run_post_launch(env, e["post_launch"], i)
                    time.sleep(2)

                xml_text = env.controller.get_accessibility_tree()
                if not xml_text:
                    raise RuntimeError("get_accessibility_tree() returned empty content")
                screenshot = env.controller.get_screenshot()
                if not screenshot:
                    raise RuntimeError("get_screenshot() returned empty content")

                with open(os.path.join(args.out_dir, filename), "w", encoding="utf-8") as f:
                    f.write(xml_text)
                with open(os.path.join(args.screenshot_dir, screenshot_filename), "wb") as f:
                    f.write(screenshot)

                row["title"] = _guess_title(xml_text, e["app_name_substrings"])
                row["xml_bytes"] = len(xml_text.encode("utf-8"))
                row["screenshot_bytes"] = len(screenshot)

                missing = _check_expected(xml_text, e.get("expect_text"))
                row["content_check"] = missing or "ok"
                if missing:
                    row["status"] = "content_missing"
                    logger.error("[%03d] %s: captured tree does not contain %r -- app launched but "
                                 "is showing the wrong state", i, key, missing.split(":", 1)[1])
                else:
                    row["status"] = "ok"
                    succeeded += 1
            except Exception as ex:
                logger.exception("[%03d] failed on %s", i, key)
                row["error"] = str(ex)
            manifest_by_index[i] = row
            _write_manifest()

        logger.info("Done: %d/%d apps captured successfully. Manifest: %s",
                    succeeded, len(indexed), manifest_path)
    finally:
        env.close()


if __name__ == "__main__":
    main()
