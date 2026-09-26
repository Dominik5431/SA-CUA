#!/bin/bash
# Launcher for labelers/selector_propose.py over a set of captures.
#
# Wraps the three things every run needs and that are easy to get wrong by hand:
#   * capture selection from manifest.json (index ranges, bot-wall filtering)
#   * ONE run id pinned outside the retry loop, so a crash-and-retry rejoins the same run
#     instead of landing as three separate ones
#   * a retry loop that gives up when an attempt stops making progress, not after a fixed count
#
# Usage:
#   ./run_selector.sh [options] <spec>...
#
# Specs (repeatable, combined as a union):
#   002              one capture by index
#   000-099          inclusive index range
#   100+             index 100 and up
#   all              every capture in the manifest
#   007_foo.xml      an exact filename (passed through if xml/ has it)
#
# Options:
#   --label NAME     run-id prefix; the id becomes NAME-<UTC stamp>   (default: v10-think-conc8)
#   --out-dir DIR    rules directory                                  (default: llm_rules_container_v10)
#   --model STR      provider|model                                   (default: vllm|Qwen/...)
#   --port N         vLLM port                                        (default: 8002)
#   --concurrency N  parallel rule generation; needs --out-dir        (default: 1)
#   --fresh          re-query the model instead of replaying saved rules (drops --resume)
#   --no-think       disable thinking (v10 and the script default have it ON)
#   --attempts N     max retry attempts                               (default: 40)
#   --run-id ID      use this exact run id instead of generating one (to extend an existing run)
#   --dry-run        print the resolved capture list and the command, run nothing
#   --               everything after this is passed straight to selector_propose.py
#
# Examples:
#   ./run_selector.sh 002                          # replay v10's saved rules for 002 under a new id
#   ./run_selector.sh --fresh --concurrency 8 000-200
#   ./run_selector.sh --label v11-foo --out-dir llm_rules_container_v11 --fresh 000-099
set -u

AT_SPI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$AT_SPI" || exit 1

LABEL="v10-think-conc8"
OUT_DIR="llm_rules_container_v10"
MODEL="vllm|Qwen/Qwen-AgentWorld-35B-A3B"
PORT=8002
CONC=1
RESUME=1
THINK=1
ATTEMPTS=40
RUN_ID=""
DRY=0
SPECS=()
EXTRA=()

while [ $# -gt 0 ]; do
    case "$1" in
        --label)        LABEL="$2"; shift 2 ;;
        --out-dir)      OUT_DIR="$2"; shift 2 ;;
        --model)        MODEL="$2"; shift 2 ;;
        --port)         PORT="$2"; shift 2 ;;
        --concurrency)  CONC="$2"; shift 2 ;;
        --attempts)     ATTEMPTS="$2"; shift 2 ;;
        --run-id)       RUN_ID="$2"; shift 2 ;;
        --fresh)        RESUME=0; shift ;;
        --no-think)     THINK=0; shift ;;
        --dry-run)      DRY=1; shift ;;
        --)             shift; EXTRA=("$@"); break ;;
        -h|--help)      sed -n '2,/^set -u/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//;$d'; exit 0 ;;
        -*)             echo "unknown option: $1 (use -- to pass flags through)" >&2; exit 2 ;;
        *)              SPECS+=("$1"); shift ;;
    esac
done

[ ${#SPECS[@]} -eq 0 ] && { echo "no capture spec given; try: $0 --help" >&2; exit 2; }

# Capture selection. Kept in python because manifest.json is the authority on which captures are
# usable -- a bot-walled capture scores against ground truth that was never labeled for the page
# actually behind it, so those are dropped here rather than being noticed later in the numbers.
# Skips are printed, never silent.
#
# `status` NO LONGER FILTERS, and the reason is corpus parity rather than a claim that the status
# is meaningless -- it is not. The only non-ok status in the manifest is `content_missing`, set by
# collect_atspi_apps.py when a capture's `expect_text` assertion fails, i.e. the app launched but
# is showing the wrong state. That check earns its keep (a Thunderbird capture once entered the
# ground truth as a mail client with zero untrusted rows because it had landed on the first-run
# wizard), and skipping those captures here was a defensible reading of it.
#
# What made it wrong in practice: the two captures it drops, 227_terminal_git_log and
# 229_terminal_ps, are hand-labeled AND carry rules in every llm_rules_container_v1x, because the
# runs that produced those directories were direct python calls that never consulted `status`. So
# this script selected 158 while every number quoted from a rules directory covered 160, and an
# ablation launched through here was silently compared against a different corpus than its own
# baseline. One selector has to win, and it has to be the one the rules directories already use.
#
# WHAT THOSE TWO ACTUALLY HOLD, measured rather than assumed: 21 rows each, of which 10 are
# content-tier and all 10 are labeled, 2 untrusted. Every text node is window chrome -- the title
# bar and 'Minimize' -- because the terminal's own output is not exposed through AT-SPI at all
# (no 'PID', no 'Initial commit' anywhere in either tree). They are nearly empty, not misleading,
# and v15's rules for both are `[]`. Including them costs two near-trivial LLM calls and keeps
# one corpus definition instead of two.
#
# UNUSABLE_STATUSES stays as the explicit hook. It is empty today; add a status only with a reason,
# and only after checking whether the affected captures carry ground truth.
SELECT=$(python3 - "${SPECS[@]}" <<'PY'
import json, os, re, sys

manifest = json.load(open("manifest.json"))
specs = sys.argv[1:]

# See the comment above the heredoc. Empty on purpose: a status is a note about what the capture
# looked like, not a verdict on whether it can be run.
UNUSABLE_STATUSES = frozenset()

def wanted(entry):
    idx = entry.get("index", -1)
    for s in specs:
        if s == "all":
            return True
        if s.endswith(".xml"):
            if s == entry["filename"]:
                return True
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", s)
        if m and int(m.group(1)) <= idx <= int(m.group(2)):
            return True
        m = re.fullmatch(r"(\d+)\+", s)
        if m and idx >= int(m.group(1)):
            return True
        if re.fullmatch(r"\d+", s) and int(s) == idx:
            return True
    return False

keep, skipped, noted = [], [], []
for e in manifest:
    if not wanted(e):
        continue
    why = None
    if e.get("bot_wall"):
        why = "bot_wall"
    elif e.get("status") in UNUSABLE_STATUSES:
        why = f"status={e.get('status')}"
    elif not os.path.exists("xml/" + e["filename"]):
        why = "no xml"
    (skipped if why else keep).append((e["filename"], why))
    if why is None and e.get("status") != "ok":
        noted.append((e["filename"], e.get("status")))

for name, why in skipped:
    print(f"SKIP {name} ({why})", file=sys.stderr)
for name, st in noted:
    print(f"NOTE {name} (status={st}) -- included anyway, for corpus parity with the rules "
          f"directories; see the comment above this heredoc", file=sys.stderr)
if not keep:
    sys.exit("no usable captures matched")
print(f"{len(keep)} capture(s), {len(skipped)} skipped", file=sys.stderr)
print(" ".join("--file " + n for n, _ in keep))
PY
)
rc=$?
[ $rc -ne 0 ] && exit $rc
N=$(echo "$SELECT" | tr ' ' '\n' | grep -c '\.xml$')

ARGS=(--model "$MODEL" --vllm_port "$PORT" --out_dir "$OUT_DIR" --policy allowlist --score)
[ "$CONC" -gt 1 ] && ARGS+=(--concurrency "$CONC")
[ "$RESUME" -eq 1 ] && ARGS+=(--resume)
[ "$THINK" -eq 1 ] && ARGS+=(--enable_thinking)
[ ${#EXTRA[@]} -gt 0 ] && ARGS+=("${EXTRA[@]}")

# The run id is generated ONCE, here, and passed to every attempt below. selector_propose.py
# defaults to a fresh timestamp per invocation, so letting it generate would split one logical
# run across every retry.
[ -z "$RUN_ID" ] && RUN_ID="$LABEL-$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p logs
LOG="logs/$RUN_ID.log"

if [ "$DRY" -eq 1 ]; then
    echo "run_id   $RUN_ID"
    echo "captures $N"
    echo "log      $LOG"
    echo "command  python labelers/selector_propose.py <$N --file args> ${ARGS[*]} --run_id $RUN_ID"
    echo
    echo "$SELECT" | tr ' ' '\n' | grep '\.xml$' | sed 's/^/  /'
    exit 0
fi

export PYTHONPATH="$AT_SPI"
export VLLM_TIMEOUT="${VLLM_TIMEOUT:-1800}"

# Timestamp every line. `ts` is not installed everywhere and `date` in a read loop is, so this is
# the portable version -- it also flushes per line, which matters when the run is backgrounded and
# watched with tail -f.
stamp() { while IFS= read -r line; do printf '%s %s\n' "$(date -u +%H:%M:%S)" "$line"; done; }

{
    echo "### run_id $RUN_ID"
    echo "### $N capture(s)  resume=$RESUME think=$THINK concurrency=$CONC  out_dir=$OUT_DIR"
    echo "### model $MODEL  port $PORT"
    for attempt in $(seq 1 "$ATTEMPTS"); do
        before=$(ls "$OUT_DIR"/*.json 2>/dev/null | wc -l)
        echo "### attempt $attempt starting at $before rule file(s)"
        # shellcheck disable=SC2086 -- $SELECT is a deliberately word-split list of --file args
        python labelers/selector_propose.py $SELECT "${ARGS[@]}" --run_id "$RUN_ID" 2>&1
        rc=${PIPESTATUS[0]}
        after=$(ls "$OUT_DIR"/*.json 2>/dev/null | wc -l)
        [ "$rc" -eq 0 ] && { echo "### COMPLETE rc=0 at $after rule file(s)"; break; }
        # Give up on a retry that added nothing: the failure is not the transient kind (a timeout,
        # a dropped connection) that retrying fixes, and looping 40 times on it just buries the
        # real error further up the log.
        [ "$after" -le "$before" ] && { echo "### GIVING UP: attempt $attempt added 0 rule files (rc=$rc)"; break; }
        echo "### attempt $attempt died rc=$rc at $after (+$((after - before))); retrying"
    done
    echo "### finished"
    echo "### mask with:  python mask_screenshot.py --run_id $RUN_ID --file <id>"
} | stamp | tee "$LOG"
