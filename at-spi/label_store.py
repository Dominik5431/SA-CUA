#!/usr/bin/env python3
"""
label_store.py -- run-indexed storage for labels/selector_llm_proposed.

WHY A DIRECTORY AND NOT A FILE
------------------------------
selector_propose.py's own header states the requirement: run-to-run non-determinism at
temperature=0.0 is real and confirmed, so different models -- *or different runs of the same
model* -- must stay distinguishable and comparable side by side. The old flat
labels/selector_llm_proposed.json merged on (file, model), which delivered only the first half:
re-running one model over the corpus silently destroyed the previous run's rows.

Extending the merge key to (file, model, run_id) is not enough on its own, because the flat file
cannot carry the cost. save_llm_labels read-modify-writes the ENTIRE json after EVERY capture --
deliberately, since that checkpointing is what stops a 16-hour sweep from losing everything to a
crash on capture 250. At 609 B/row and ~105 rows/file a 300-capture run is ~19 MB, so with
accumulation each new run re-serializes every previous run's rows 300 times: 11.5 GB of rewrite
for run 2, 57.7 GB by run 10. The cost grows with history depth forever and cannot be traded away
without giving up crash safety.

One file per run fixes four things at once:
  1. nothing is ever lost -- no run rewrites another run's file;
  2. checkpoint cost is flat at ~19 MB no matter how many runs came before;
  3. retention is `rm`, not a filtering script;
  4. the concurrency race disappears. Two runs sharing one JSON lose each other's rows (classic
     lost update) -- which is why the 2026-08-20 parallel v2 run had to be launched with
     --no-save_labels. Under per-run files both runs can just save.

LAYOUT
    labels/selector_llm_proposed/
        20260819T143422Z__Qwen_Qwen-AgentWorld-35B-A3B.json        # rows
        20260819T143422Z__Qwen_Qwen-AgentWorld-35B-A3B.meta.json   # run manifest + scores
    labels/selector_llm_proposed.json                              # legacy flat file, read-only

The loader reads BOTH, so nothing has to be migrated before the tools work.

This module is shared by the producer (labelers/selector_propose.py) and by every consumer
(review_benchmark.py, mask_screenshot.py). It is deliberately one implementation: a consumer that
kept reading only the flat file would go on showing the pre-migration run forever without ever
reporting an error, which is the exact silent-staleness failure this layout exists to end.
"""
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS_DIR = os.path.join(HERE, "labels")

RUN_INDEXED_SOURCE = "selector_llm_proposed"

LLM_LABELS_DIR = os.path.join(LABELS_DIR, RUN_INDEXED_SOURCE)
LEGACY_LLM_LABELS_PATH = os.path.join(LABELS_DIR, RUN_INDEXED_SOURCE + ".json")

LEGACY_FLAT_RUN_ID = "legacy-flat"

RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"


def new_run_id() -> str:
    """A fresh UTC run id. Generated ONCE per logical run, never per capture."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime(RUN_ID_FORMAT)


def model_tag(model: "str | None") -> str:
    """Filesystem-safe model name. Must stay identical to the tag selector_propose.py uses for
    --out_dir rule filenames, so a run's rules and its rows carry the same spelling of the model."""
    return re.sub(r"[^\w.-]+", "_", model) if model else "unknown"


RUNS_SUBDIR = "_runs"


def capture_stem(capture: str) -> str:
    """`000_stackoverflow.com.xml` -> `000_stackoverflow.com`."""
    return capture[:-4] if capture.endswith(".xml") else capture


def capture_path(capture: str, model: "str | None") -> str:
    """Rows file for ONE capture under one model -- the unit of storage.

    Keyed by capture, not by run, and that is the whole point. A run file held every capture the
    invocation touched and was rewritten in full on each save, so two invocations sharing a
    --run_id silently clobbered each other: a v6 sweep over ten captures left rows for two, while
    llm_rules_container_v6/ -- which has always been one file per capture -- kept all ten. Rows
    from every run coexist inside the file, each tagged with its own run_id, so history is kept
    without letting one capture's write touch another's.
    """
    return os.path.join(LLM_LABELS_DIR, f"{capture_stem(capture)}__{model_tag(model)}.json")


def run_meta_path(run_id: str, model: "str | None") -> str:
    """Manifest for one run. Run-level metadata (config, per-file scores, git sha) is still a
    property of the RUN, so it stays keyed that way -- just moved into a subdirectory so the main
    directory listing is one entry per capture."""
    return os.path.join(LLM_LABELS_DIR, RUNS_SUBDIR, f"{run_id}__{model_tag(model)}.meta.json")


def capture_files() -> "list[str]":
    """Every per-capture rows file, sorted. Skips the manifest subdirectory."""
    return sorted(p for p in glob.glob(os.path.join(LLM_LABELS_DIR, "*.json"))
                  if not p.endswith(".meta.json"))


def save_capture_rows(capture: str, model: "str | None", run_id: str,
                      rows: "list[dict]") -> str:
    """Merge one run's rows for ONE capture into that capture's file.

    Replaces only the (model, run_id) group; every other run's rows in the file survive. This is a
    read-modify-write, but of one capture's file (~100 KB) rather than the whole corpus, so it does
    not reacquire the cost that made the old cross-run merge untenable.
    """
    path = capture_path(capture, model)
    existing = []
    if os.path.exists(path):
        try:
            existing = [r for r in _read_json(path)
                        if not (r.get("run_id") == run_id and r.get("model") == model)]
        except (OSError, ValueError):
            existing = []
    for r in rows:
        r.setdefault("run_id", run_id)
    write_json_atomic(path, existing + list(rows))
    return path


_TIMESTAMP_RE = re.compile(r"\d{8}T\d{6}Z")


def run_sort_key(run_id: "str | None") -> tuple:
    """Chronological order for run ids, oldest first. This decides which run wins
    `narrow_to_run`'s default, so getting it wrong silently shows the wrong results.

    Plain lexicographic sort gets it wrong three times over:
      * migrated ids are prefixed `legacy-` and 'l' > '2', so a 2026 run would sort before a
        legacy one that actually ran earlier -- strip the prefix and sort on the timestamp;
      * real run ids are not bare timestamps, they carry a human label in front of one
        ("v10-think-conc8-20260824T151949Z"), and comparing those strings compares the LABEL
        first: 'v10-...' < 'v9-...' because '1' < '9', so v10 loses to a v9 that ran earlier.
        Sort on the embedded stamp, wherever in the id it sits;
      * ids with no timestamp at all ('legacy-flat', 'legacy-unknown', 'v3-prompt-000', or any
        string a --run_id was pinned to by hand) have no date to compare, and under a plain sort
        a name beginning with a letter beats every real timestamp, which would make the
        un-migrated flat file outrank a genuine new run.

    So: dateless ids sort BELOW every timestamped one (tier 0 vs 1) -- defaulting to a run whose
    date is known is the defensible choice -- and the pre-indexing flat file, which is by
    definition older than any indexed run, sorts below even those. Within a tier the full id
    breaks ties, so the order is total and stable.
    """
    if not run_id:
        return (-2, "", "", 0)
    if run_id == LEGACY_FLAT_RUN_ID:
        return (-1, "", "", 0)
    legacy = run_id.startswith("legacy-")
    body = run_id[len("legacy-"):] if legacy else run_id
    stamp = _TIMESTAMP_RE.search(body)
    if stamp:
        return (1, stamp.group(0), body, 0 if legacy else 1)
    return (0, "", body, 0 if legacy else 1)


def _read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: str, obj) -> None:
    """Write via tmp + os.replace.

    At ~19 MB the write window is not negligible, and this is called after every capture: a kill
    landing inside a plain open("w") leaves a truncated JSON that every later run and every
    consumer then fails to parse. os.replace is atomic within a filesystem, so a reader sees
    either the old file or the new one, never a half-written one.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def available_sources() -> "list[str]":
    """--source values: every flat labels/*.json plus the run-indexed directory."""
    names = {os.path.splitext(os.path.basename(p))[0]
             for p in glob.glob(os.path.join(LABELS_DIR, "*.json"))}
    if os.path.isdir(LLM_LABELS_DIR):
        names.add(RUN_INDEXED_SOURCE)
    return sorted(names)


def list_runs(source: str = RUN_INDEXED_SOURCE) -> "list[dict]":
    """Run manifests, oldest first, read from the _runs/ subdirectory.

    Manifests are the only per-RUN artefact left; the rows themselves are one file per capture.
    A run whose manifest was lost still shows up, reconstructed from the run_ids present in the
    capture files, so a crashed run is never invisible.
    """
    if source != RUN_INDEXED_SOURCE:
        return []
    runs, seen = [], set()
    for meta_path in sorted(glob.glob(os.path.join(LLM_LABELS_DIR, RUNS_SUBDIR, "*.meta.json"))):
        meta = {}
        try:
            meta = _read_json(meta_path)
        except json.JSONDecodeError as e:
            meta = {"error": f"unreadable manifest: {e}"}
        stem = os.path.basename(meta_path)[:-len(".meta.json")]
        meta.setdefault("run_id", stem.split("__", 1)[0])
        meta.setdefault("model", None)
        meta["meta_path"] = meta_path
        runs.append(meta)
        seen.add(meta["run_id"])
    for path in capture_files():
        try:
            for r in _read_json(path):
                rid = r.get("run_id")
                if rid and rid not in seen:
                    seen.add(rid)
                    runs.append({"run_id": rid, "model": r.get("model"),
                                 "note": "no manifest on disk; recovered from capture rows"})
        except (OSError, ValueError):
            continue
    if os.path.exists(LEGACY_LLM_LABELS_PATH):
        runs.append({"run_id": LEGACY_FLAT_RUN_ID, "model": None,
                     "rows_path": LEGACY_LLM_LABELS_PATH,
                     "note": "un-migrated flat file; may hold several models' pre-indexing runs"})
    runs.sort(key=lambda r: run_sort_key(r.get("run_id")))
    return runs


def load_rows(source: str, run_id: "str | None" = None) -> "list[dict]":
    """All rows for --source.

    For the run-indexed source this reads every per-capture file AND the legacy flat file, so an
    un-migrated corpus keeps working unchanged. Rows are stamped with the run_id they carry (rows
    written before indexing get LEGACY_FLAT_RUN_ID) so a downstream filter, column, or
    default-to-newest never has to guess.

    `run_id` filters at load time rather than after: with many runs' rows now coexisting inside
    each capture file, parsing everything to then discard all but one is the difference between a
    fast tool and a slow one.
    """
    if source != RUN_INDEXED_SOURCE:
        path = os.path.join(LABELS_DIR, f"{source}.json")
        if not os.path.exists(path):
            return []
        return _read_json(path)

    rows = []
    for path in capture_files():
        try:
            for r in _read_json(path):
                r.setdefault("run_id", None)
                if run_id and r.get("run_id") != run_id:
                    continue
                rows.append(r)
        except (OSError, ValueError):
            continue
    if os.path.exists(LEGACY_LLM_LABELS_PATH) and (not run_id or run_id == LEGACY_FLAT_RUN_ID):
        for r in _read_json(LEGACY_LLM_LABELS_PATH):
            r.setdefault("run_id", LEGACY_FLAT_RUN_ID)
            if run_id and r["run_id"] != run_id:
                continue
            rows.append(r)
    return rows


def narrow_to_run(rows: "list[dict]", run_id: "str | None" = None,
                  quiet: bool = False) -> "tuple[list[dict], str | None]":
    """Reduce rows to exactly one run, returning (rows, chosen run_id).

    With no --run_id and several runs present, the NEWEST wins and the choice is announced on
    stderr. Erroring instead would break every existing `--source selector_llm_proposed --model
    ...` invocation the moment a second run lands -- and silently mixing runs would draw two
    verdicts for the same element on one screenshot, which is the failure --model already exists
    to prevent.
    """
    present = sorted({r.get("run_id") for r in rows if r.get("run_id")}, key=run_sort_key)
    if run_id:
        return [r for r in rows if r.get("run_id") == run_id], run_id
    if len(present) <= 1:
        return rows, (present[0] if present else None)
    chosen = present[-1]
    if not quiet:
        print(f"[label_store] {len(present)} runs present {present}; showing newest: {chosen}. "
              f"Pass --run_id to pick another, or --list_runs to see them all.", file=sys.stderr)
    return [r for r in rows if r.get("run_id") == chosen], chosen


def runs_covering(rows: "list[dict]", file_prefix: "str | None") -> "list[str]":
    """Which runs in `rows` (pre-narrowing) hold any capture matching `file_prefix`.

    Runs cover different subsets of the corpus -- a sweep that crashed at capture 40, a two-file
    probe -- so narrowing to the newest run and then filtering by --file can come up empty for a
    capture that other runs do have. Without this the tool would say only "no rows match the given
    filters", and the run narrowing, which is the actual cause, would be invisible.
    """
    if not file_prefix:
        return []
    return sorted({r.get("run_id") for r in rows
                   if r.get("run_id") and r.get("file", "").startswith(file_prefix)},
                  key=run_sort_key)


def available_models(source: str, run_id: "str | None" = None) -> "list[str]":
    """Distinct row["model"] values present in a source."""
    return sorted({r["model"] for r in load_rows(source, run_id) if r.get("model")})


def format_runs(runs: "list[dict]") -> str:
    """One line per run for --list_runs."""
    if not runs:
        return "(no runs found)"
    out = [f"{'run_id':26} {'model':34} {'files':>6} {'policy':10} {'rules_dir':22} totals",
           "-" * 118]
    for run in runs:
        files = f"{len(run['files'])}" if isinstance(run.get("files"), dict) else "-"
        totals = run.get("totals")
        if isinstance(totals, dict):
            tot = (f"TP={totals.get('tp')} FP={totals.get('fp')} "
                   f"TN={totals.get('tn')} FN={totals.get('fn')}")
        else:
            tot = "(not recorded)"
        policy = run.get("policy") or "/".join(run.get("policy_effective") or []) or "-"
        out.append(f"{str(run.get('run_id')):26} {str(run.get('model'))[:34]:34} "
                   f"{files:>6} {policy:10} "
                   f"{str(run.get('rules_dir') or '-')[:22]:22} {tot}")
        if run.get("note"):
            out.append(f"{'':26} note: {run['note']}")
    return "\n".join(out)


if __name__ == "__main__":
    print(format_runs(list_runs()))
