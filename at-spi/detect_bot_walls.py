"""Find captures that landed on an anti-bot challenge instead of the page we asked for.

Such a capture is worse than a missing one: it is a well-formed screenshot + a11y tree + DOM of a
CAPTCHA, so nothing downstream flags it, and any labels put on it describe a challenge widget
rather than the site. This scans every frame of every capture for signatures only a
challenge/block page emits.

Two things keep the false-positive rate down:

  * A *title* match is strong on its own -- a page titled "Access Denied - Macy's" is one.
  * A match in a frame URL or in body text is strong only on a near-empty page (`--max-nodes`,
    default 400). Without that rule the Wikipedia article on TLS trips "access denied" (it is in
    the alert-code table), a Chinese blog trips it in a post title, and Quora trips the Cloudflare
    URL pattern because it embeds an *invisible* Turnstile widget on a perfectly good page. All
    three are real pages and all three were flagged before this rule existed.

Usage:
    python detect_bot_walls.py                 # report
    python detect_bot_walls.py --indices       # bare index list, to feed --indices of the collector
    python detect_bot_walls.py --wrong-content # also list captures that loaded but show the wrong page
    python detect_bot_walls.py --mark          # write the verdict into manifest.json (dry run)
    python detect_bot_walls.py --mark --apply  # ...and save it, excluding them from the corpus
"""
import argparse
import glob
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))

WALL_URL = [
    (r"/cdn-cgi/challenge-platform", "cloudflare"),
    (r"captcha-delivery\.com|geo\.captcha", "datadome"),
    (r"\.px-cloud\.net|px-captcha|perimeterx", "perimeterx"),
    (r"/blocked\?url=", "blocked-redirect"),
    (r"google\.com/sorry", "google-sorry"),
    (r"awswaf|aws-waf", "aws-waf"),
    (r"incapsula|_Incapsula_Resource", "imperva"),
]
WALL_TEXT = [
    (r"Enable JavaScript and cookies to continue", "cloudflare"),
    (r"You have been blocked|Sorry, you have been blocked", "blocked"),
    (r"Human verification|Press & Hold", "perimeterx"),
    (r"Prove your humanity|verify that you'?re not a robot|not a robot", "captcha"),
    (r"unusual traffic from your (computer )?network", "google-sorry"),
    (r"Access Denied|Access to this page has been denied", "access-denied"),
    (r"Robot or human\?|Robot Check|Bot or Not\?", "captcha"),
    (r"Pardon Our Interruption", "interstitial"),
    (r"Something doesn'?t feel quite right", "soft-block"),
    (r"Verification Required|Slide right to secure your access", "datadome"),
]
WRONG_CONTENT_TITLE = [
    (r"^Page not found|404 Not Found|\b404\b.*not found", "404"),
    (r"Select your Country|International: Select", "country-interstitial"),
    (r"^Just a moment", "challenge"),
]

WALL_URL = [(re.compile(p, re.I), n) for p, n in WALL_URL]
WALL_TEXT = [(re.compile(p, re.I), n) for p, n in WALL_TEXT]
WRONG_CONTENT_TITLE = [(re.compile(p, re.I), n) for p, n in WRONG_CONTENT_TITLE]


def scan(max_nodes: int = 400):
    """Returns (walls, wrong_content), each a list of (index, domain, kinds, top_nodes, url)."""
    rows = json.load(open(os.path.join(_HERE, "manifest.json"), encoding="utf-8"))
    walls, wrong = [], []
    for r in rows:
        stem = os.path.splitext(r.get("filename") or "")[0]
        geom_path = os.path.join(_HERE, "dom_geom", f"{stem}.json")
        if not stem or not os.path.exists(geom_path):
            continue
        try:
            g = json.load(open(geom_path, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        title = r.get("title") or ""

        top_nodes = 0
        for f in g.get("frames", []):
            if f.get("is_top"):
                top_nodes = len(f.get("nodes", []))
        small = top_nodes < max_nodes

        kinds = set()
        for rx, name in WALL_TEXT:
            if rx.search(title):
                kinds.add(name)
        for f in g.get("frames", []):
            u = f.get("url") or ""
            txt = " ".join((n.get("own_text") or "") for n in f.get("nodes", []))
            if not small:
                continue
            for rx, name in WALL_URL:
                if rx.search(u):
                    kinds.add(name)
            for rx, name in WALL_TEXT:
                if rx.search(txt):
                    kinds.add(name)
            if re.search(r"recaptcha/api2/(anchor|bframe)", u):
                kinds.add("recaptcha")

        if kinds:
            walls.append((r["index"], r["domain"], sorted(kinds), top_nodes, r["url"]))
            continue
        for rx, name in WRONG_CONTENT_TITLE:
            if rx.search(title):
                wrong.append((r["index"], r["domain"], [name], top_nodes, r["url"]))
                break
    return sorted(walls), sorted(wrong)


def mark(walls: list, apply: bool = False) -> None:
    """Write each walled capture's verdict into manifest.json as `bot_wall`, and CLEAR the field on
    everything else.

    Clearing matters as much as setting: a capture that was walled and has since been re-collected
    successfully must come back into the corpus on its own, without anyone remembering to undo a
    marker. So this is a full rewrite of the field from the current scan, never an accumulation.

    Non-destructive: the XML, screenshot and DOM are untouched, and deleting the field restores the
    capture. `labeling_store.EXCLUDE_BOT_WALLS = False` turns the exclusion off wholesale.
    """
    path = os.path.join(_HERE, "manifest.json")
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    by_index = {i: (d, k, n, u) for i, d, k, n, u in walls}
    added, cleared = [], []
    for r in rows:
        idx = r.get("index")
        was = r.get("bot_wall")
        if idx in by_index:
            d, kinds, n, u = by_index[idx]
            r["bot_wall"] = {"vendor": "+".join(sorted(kinds)), "top_nodes": n, "url": u}
            if not was:
                added.append((idx, d, "+".join(sorted(kinds))))
        elif was:
            r.pop("bot_wall", None)
            cleared.append((idx, r.get("domain")))
    for idx, d, kinds in added:
        print(f"  + {idx:03d}  {d:<32} {kinds}")
    for idx, d in cleared:
        print(f"  - {idx:03d}  {d:<32} no longer walled -- back in the corpus")
    total = sum(1 for r in rows if r.get("bot_wall"))
    print(f"\n{len(added)} newly marked, {len(cleared)} cleared, {total} excluded in total "
          f"of {len(rows)} captures")
    if not apply:
        print("dry run -- nothing written. re-run with --apply")
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
    print(f"wrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--indices", action="store_true",
                    help="print only a comma-separated index list, for --indices of the collector")
    ap.add_argument("--wrong-content", action="store_true",
                    help="also report captures that loaded fine but show the wrong page")
    ap.add_argument("--max-nodes", type=int, default=400,
                    help="a URL/text signature only counts on a top frame smaller than this")
    ap.add_argument("--mark", action="store_true",
                    help="record the verdict in manifest.json as `bot_wall`, which "
                         "labeling_store.corpus_files() then excludes from the corpus")
    ap.add_argument("--apply", action="store_true", help="with --mark: actually write manifest.json")
    args = ap.parse_args()

    walls, wrong = scan(args.max_nodes)
    if args.mark:
        mark(walls, apply=args.apply)
        return
    if args.indices:
        picked = walls + (wrong if args.wrong_content else [])
        print(",".join(str(i) for i, *_ in sorted(picked)))
        return

    for i, d, k, n, u in walls:
        print(f"{i:03d}  {d:<30} {'+'.join(k):<30} top_nodes={n:<6} {u[:58]}")
    print(f"\n{len(walls)} bot-walled")
    if args.wrong_content:
        print()
        for i, d, k, n, u in wrong:
            print(f"{i:03d}  {d:<30} {'+'.join(k):<30} top_nodes={n:<6} {u[:58]}")
        print(f"\n{len(wrong)} wrong-content")


if __name__ == "__main__":
    main()
