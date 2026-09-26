# Results viewer

A small stdlib-only web app for browsing `results/`.

```bash
python3 viz/server.py                 # http://localhost:8500/
python3 viz/server.py --port 9000     # prefer another port
python3 viz/server.py --results /other/results
python3 viz/server.py --examples evaluation_examples/examples
python3 viz/server.py --host 127.0.0.1        # local only (default binds 0.0.0.0)
python3 viz/server.py --strict-port           # fail instead of falling forward
```

If the preferred port is taken the server walks upward to the next free one and
says so on startup. Nothing is installed. It only reads `results/` — except the
optional **Save Trajectory** page, which copies screenshots you select into a
subfolder of `viz/`.

## What it shows

Cascading dropdowns for **action space → observation type → model → domain →
task** (plus a substring filter for the task list, since some models have
several hundred). Each dropdown only offers values that exist under the
selections above it.

For the selected task:

- the user prompt from `config.json`, plus `meta_data` / `tags` chips
- the injected adversarial content from `adversary_config` — inline text for
  reddit/gitlab, the NPC `strategy_hint` for rocketchat, and the text of the
  uploaded file for ownCloud (PDFs are extracted if `pymupdf` is importable)
- `result.txt`, `adversary_result.txt` and the two counters in `check.json`
- a step-by-step view of the trajectory: screenshot, model reasoning, model
  output, the executed pyautogui action, and any execution error
- for **SA-Agent** runs, a richer per-*turn* view (see below)

Navigate steps with the buttons, the slider, or ←/→/Home/End.

## Save trajectory

Click **Save Trajectory** in the header (or open `/save.html`) to see every
screenshot from the selected trajectory as a gallery. The page pre-selects the
task you were viewing when possible, and the **Run** dropdown lets you pick one
run or *all runs* of that task. Tick the images you want (all are ticked by
default), enter a subfolder name, set the LaTeX image width, and press
**Save + LaTeX**. The chosen files are copied into `viz/<name>/`, with `_1`,
`_2`, … suffixes added only if two selected images share a filename.

A `trajectory.tex` snippet is written into the same folder. It is meant to be
`\input` into an existing document (so it emits only the part that imports the
images, no preamble/documentclass), and it assumes the whole `viz/<name>/`
folder is added to the project. The snippet shows the model name and user
instruction, then each screenshot with its step number as a caption and a small
`$\downarrow$` between images. `viz/latex_creator_utils.py` is the standalone
generator and can also be used from the command line.

## Incomplete runs

`run.py` writes `config.json`, `result.txt`, `adversary_result.txt` and
`check.json` only *after* the agent loop and both evaluations succeed
(`lib_run_single.py:26-43`). A run that crashes mid-loop therefore leaves only
`traj.jsonl`, `runtime.log` and `response.json`, and has no prompt or scores to
show.

For those the viewer reads the prompt back from the task definition under
`evaluation_examples/examples/{domain}/{task_id}.json`, falling back to any
suite folder holding that id (results recorded under `reddit` whose definition
lives in `reddit_extended`). The sidebar says so when this happens, and the
score cards show `—` rather than a fabricated 0.

Do not read `[Instruction]:` out of `runtime.log` for this — the log handler is
swapped after the next episode starts, so a crashed episode's log ends with the
*following* task's instruction.

## Runs

`traj.jsonl` and `runtime.log` accumulate across reruns of the same task, so the
viewer splits them into runs wherever the step counter restarts. The newest run
is selected by default; the **Run** dropdown exposes the older ones. Step 0 is
the pre-action screenshot, matched by picking the newest `step_0_*.png` at or
before the run's first action.

`response.json` is overwritten on every rerun, so it is only used for the newest
run; older runs fall back to the `[RESPONSE_FIRST]` section of `runtime.log`.

## SA-Agent runs

A SA-Agent (P-LLM/Q-VLM) directory is detected by its own artifacts
(`action_list_trace.jsonl` / `planner_thinking.jsonl` / a `masked/` folder) and
is reconstructed at **planner-turn** granularity rather than env-step, since one
env step spans many planner turns (query_screen / find / a terminal command / an
executor action) and `runtime.log` only captures a fraction of them.

Each turn shows:

- the **masked** screenshot the agent was actually shown (`masked/turn_NNN.png`),
  with a toggle to the **unmasked** ground-truth frame (`unmasked/turn_NNN.png`)
- the planner's full reasoning and output for that turn (`planner_thinking.jsonl`)
- a **Masking** panel — coverage % and row counts from `mask_stats.jsonl`
- the **Action list** offered to the planner (`action_list_trace.jsonl`), i.e.
  the trust/zone/capability-tagged interactable elements, plus the raw JSON

Below the per-turn panels, a persistent **Run-level trace** section shows the
executed env actions (`traj.jsonl`), a masking summary, the trust-gate decisions
(`policy_trace.jsonl`), endorsement events (`endorse_log.jsonl` if present),
the planner conversation with its tool calls (`planner_history.json`, its
redacted tool outputs resolved through `memory.json`), and the resolved
`memory.json` values.

SA artifacts split two ways across in-place reruns: the turn-keyed `*.jsonl`
logs are append-only (the viewer splits them into runs on the turn restart, like
`traj.jsonl`), while `masked/`, `unmasked/`, `planner_history.json` and
`memory.json` are overwritten, so their contents belong to the newest run only —
older runs show turns without screenshots. `policy_trace.jsonl` has no per-run
key, so its panel shows every decision in the directory (noted in the header).

A task whose `traj.jsonl` holds only an error record is shown as a crashed run
with its traceback.

## Endpoints

| path | purpose |
| --- | --- |
| `/api/index` | every indexed task with its five path components |
| `/api/task?p=<relpath>` | parsed detail for one task directory |
| `/img?p=<relpath>` | a screenshot |
| `/api/rescan` | drop the index cache after a new run lands |
| `/api/save-images` (POST) | copy `{dir, images[]}` into `viz/<dir>/` |

The index is built once at startup, so runs that land while the server is up
will not appear until you hit **Rescan** in the header (or call `/api/rescan`).
