#!/usr/bin/env bash
#
# One-time (idempotent) migration of evaluation_examples/examples/ from
# "one directory per platform" to "one directory per suite".
#
# run.py only ever uses the domain -- the examples/ subdirectory name -- as a
# path segment: the config path (run.py:700) and the result path (run.py:723).
# Everything semantic dispatches on task_config["platform"] (desktop_env.py
# :748-846).  So a suite directory costs nothing at run time, and it buys two
# things: results separate themselves per suite instead of colliding, and the
# directory name stops having to mean two things at once.
#
#   before                       after
#   examples/reddit/     365     examples/reddit/            288  RTC
#                                examples/reddit_extended/     5  hand-added
#                                examples/wasp_reddit/        72  WASP port
#   examples/owncloud/   304     examples/owncloud/          288  RTC
#                                examples/owncloud_extended/  16  hand-added
#   examples/rocketchat/ 288     examples/rocketchat/        288  RTC
#   examples/gitlab/      96     examples/wasp_gitlab/        96  WASP port
#
#   test_all.json            ->  test_all_rtc.json
#   test_all_gitlab.json     ->  test_all_wasp_gitlab.json
#   test_all_reddit.json         rebuilt: 365 -> 288
#   test_all_owncloud.json       rebuilt: 304 -> 288
#   test_all_wasp_reddit.json    new
#
# The *_fixed twins are NOT built here -- they are derived output, built (and
# rebuilt) by make_rtc_fixed.sh.
#
# The matching source-side change is the "domain" field on the raw goals, so
# `bash generate_config.sh` regenerates straight into this layout instead of
# undoing it (see domain_of() in generate_config.py).
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EX="$REPO_ROOT/evaluation_examples"
DRY=0

usage() {
    cat <<EOF
usage: ${0##*/} [-n]

  -n  dry run: print what would move, change nothing
EOF
}

while getopts ":nh" opt; do
    case "$opt" in
        n) DRY=1 ;;
        h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

[ -d "$EX/examples" ] || { echo "error: no $EX/examples" >&2; exit 1; }

IN_GIT=0
git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1 && IN_GIT=1

# Move a path, preserving git history when the source is tracked.
relocate() {
    local src="$1" dst="$2"
    [ -e "$src" ] || return 0
    if [ "$DRY" -eq 1 ]; then
        echo "    would move ${src#"$EX/"} -> ${dst#"$EX/"}"
        return 0
    fi
    mkdir -p "$(dirname "$dst")"
    if [ "$IN_GIT" -eq 1 ] && git -C "$REPO_ROOT" ls-files --error-unmatch "$src" >/dev/null 2>&1; then
        git -C "$REPO_ROOT" mv -f "$src" "$dst"
    else
        mv -f "$src" "$dst"
    fi
}

# ------------------------------------------------------------- suite split ---
# Which examples belong to which suite.  These are the only three rules; keep
# them in sync with the classification comment in make_rtc_fixed.sh.
#   wasp      basename starts with own_wasp_
#   extended  reddit: CUSTOM  |  owncloud: an injection-rendering variant
WASP_RE='^own_wasp_'
REDDIT_EXTENDED_RE='CUSTOM'
OWNCLOUD_EXTENDED_RE='_(spacing|fontsize|font|rotation)_'

split_out() {   # split_out <from-domain> <to-domain> <basename-regex>
    local from="$1" to="$2" re="$3" moved=0 f base
    [ -d "$EX/examples/$from" ] || return 0
    while IFS= read -r f; do
        base="$(basename "$f")"
        [[ "$base" =~ $re ]] || continue
        relocate "$f" "$EX/examples/$to/$base"
        moved=$((moved + 1))
    done < <(find "$EX/examples/$from" -maxdepth 1 -name '*.json' | sort)
    [ "$moved" -eq 0 ] || echo "  $from -> $to: $moved examples"
}

echo "splitting suites out of examples/"
split_out reddit   wasp_reddit       "$WASP_RE"
split_out reddit   reddit_extended   "$REDDIT_EXTENDED_RE"
split_out owncloud owncloud_extended "$OWNCLOUD_EXTENDED_RE"

# examples/gitlab is entirely WASP -- there is no RTC gitlab suite -- so the
# whole directory becomes wasp_gitlab rather than being split.
if [ -d "$EX/examples/gitlab" ]; then
    stray=$({ find "$EX/examples/gitlab" -maxdepth 1 -name '*.json' -printf '%f\n' | grep -cv "$WASP_RE" || true; })
    if [ "$stray" -ne 0 ]; then
        echo "  error: examples/gitlab holds $stray non-WASP example(s); not moving it" >&2
        exit 1
    fi
    split_out gitlab wasp_gitlab "$WASP_RE"
    [ "$DRY" -eq 1 ] || rmdir "$EX/examples/gitlab" 2>/dev/null || true
fi

# ----------------------------------------------------------------- indexes ---
# test_all.json is the original RTC 864 and is not produced by
# generate_config.sh; rename it so "rtc" names the published benchmark and
# leaves test_all_<domain>.json free to mean exactly one domain.
if [ -f "$EX/test_all.json" ] && [ ! -f "$EX/test_all_rtc.json" ]; then
    relocate "$EX/test_all.json" "$EX/test_all_rtc.json"
    echo "  test_all.json -> test_all_rtc.json"
fi
if [ -f "$EX/test_all_gitlab.json" ] && [ ! -f "$EX/test_all_wasp_gitlab.json" ]; then
    relocate "$EX/test_all_gitlab.json" "$EX/test_all_wasp_gitlab.json"
    echo "  test_all_gitlab.json -> test_all_wasp_gitlab.json"
fi

if [ "$DRY" -eq 1 ]; then
    echo "dry run: indexes not rewritten"
    exit 0
fi

# Rebuild every per-domain index from what the directory now holds, and rewrite
# the multi-domain indexes to the ids that survived the split.
python3 - "$EX" <<'PY'
import json, os, sys

ex = sys.argv[1]
examples = os.path.join(ex, "examples")

# Suite directories this layout owns.  The *_fixed twins are make_rtc_fixed.sh's.
domains = [
    "reddit", "owncloud", "rocketchat",
    "reddit_extended", "owncloud_extended",
    "wasp_reddit", "wasp_gitlab",
]

have = {}
for domain in domains:
    path = os.path.join(examples, domain)
    if not os.path.isdir(path):
        continue
    have[domain] = sorted(
        f[:-5] for f in os.listdir(path) if f.endswith(".json")
    )

def write(name, index):
    index = {d: ids for d, ids in index.items() if ids}
    if not index:
        return
    with open(os.path.join(ex, name), "w") as fh:
        json.dump(index, fh, indent=4)
        fh.write("\n")
    print("  %-32s %s" % (name, ", ".join(
        "%s %d" % (d, len(ids)) for d, ids in index.items())))

print("rebuilding indexes")
for domain, ids in have.items():
    write("test_all_%s.json" % domain, {domain: ids})

write("test_all_rtc.json", {d: have.get(d, []) for d in ("reddit", "owncloud", "rocketchat")})
write("test_all_rtc_extended.json", {d: have.get(d, []) for d in ("reddit_extended", "owncloud_extended")})
write("test_all_wasp.json", {d: have.get(d, []) for d in ("wasp_reddit", "wasp_gitlab")})

# Pre-existing multi-domain index: keep it, minus the ids that left the domain.
combo = os.path.join(ex, "test_all_rocketchat_owncloud.json")
if os.path.isfile(combo):
    with open(combo) as fh:
        index = json.load(fh)
    write("test_all_rocketchat_owncloud.json",
          {d: [i for i in ids if i in set(have.get(d, []))] for d, ids in index.items()})
PY

echo "done -- now run ./make_rtc_fixed.sh to (re)build the *_fixed twins"
