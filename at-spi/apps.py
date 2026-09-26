"""
apps.py -- the desktop-app half of the at-spi corpus, sibling to sites.py.

Rewritten alongside sites.py, for the same reason and to the same shape: a small number of apps
captured *deeply*, instead of many apps captured once each. The previous list was 15 entries, 15
distinct apps, one capture apiece -- which cannot support either of the things the corpus now has
to measure (k-of-N cross-capture corroboration, in-domain generalization with a held-out split).

TWO FIELDS WHERE THERE USED TO BE ONE. `key` is the per-capture id and still names the output file
({index:03d}_{key}.xml). `app` is new and is the *corroboration domain* -- the thing that goes into
the manifest's `domain` column, shared by every capture of the same application. Without the split,
eight Thunderbird captures would have looked like eight one-capture domains and every corroboration
threshold would have silently passed on a sample size of one.

MOST OF THE OLD ENTRIES WERE CAPTURED EMPTY. Their own content_note fields said so: thunderbird
"no mail account configured", terminal "empty shell prompt", vlc "no media loaded", impress
"chrome-only". Thunderbird was the expensive one -- it scored 76 rows and *zero* untrusted in the
old ground truth, which does not mean a mail client is trusted, it means the capture caught the
first-run wizard. Message bodies are the single most important untrusted surface an OS agent meets,
and the corpus did not contain one. This file now seeds a real Local Folders mailbox (see
THUNDERBIRD_*) before Thunderbird launches.

That generalizes into the app-side version of sites.py's "deep links carry state, not just
identity": an app sitting at its start screen is the least informative state it has. Entries below
open a specific document, select a specific message, run a search, switch to a folder, scroll a log
-- via `post_launch`, a small action list the collector replays after the window settles.

`expect_text` IS THE POINT OF FAILURE INSURANCE. Every content-bearing entry declares substrings
that must appear in the captured tree. A capture that lands on a wizard, an empty document, or a
crash dialog is a well-formed XML file plus a plausible screenshot, and nothing downstream notices
-- the same failure mode detect_bot_walls.py exists to catch on the web side, which is exactly how
Thunderbird's empty capture survived into the ground truth. A missed expectation marks the manifest
row `content_check: "missing:<substring>"` and does not count as a successful capture.

WHAT IS DELIBERATELY *NOT* HERE: the chrome-only utilities are not gone but demoted to the
`holdout_app` category. Like sites.py's holdout_docs block, they measured 0.0% untrusted in the old
corpus, so they cannot produce a false negative by construction -- they price over-masking and
nothing else, and must never be proposal input or subject to a corroboration gate, which on a page
with no untrusted rows can only withdraw trust at pure FP cost.

Depth per app, and what each depth supports:

    thunderbird ..... 7   email bodies, quoted reply chains, a search    corroboration + split
    vscode .......... 6   code, logs, an explorer tree, search results   corroboration + split
    libreoffice ..... 6   prose documents, spreadsheets                  corroboration + split
    nautilus ........ 5   file names in two view modes, search           corroboration + split
    terminal ........ 5   command output -- wholly untrusted             corroboration + split
    gedit ........... 4   same content as vscode, different toolkit      corroboration
    evince .......... 3   the same prose rendered as PDF                 corroboration
    file_roller ..... 2   archive member names                           corroboration
    holdout_app ..... 6   chrome-only, FP-only controls                  neither, by design

gedit vs vscode and evince vs writer are on purpose: the same fixture content rendered by a
different toolkit (GTK vs Electron, live text vs PDF text layer) is the app-side equivalent of
sites.py's cross-host holdout -- it tests whether a rule captured the content's shape or one
toolkit's widget names.

FIXTURES / FIXTURE_COMMANDS below own the sample content, which used to be hard-coded inside
collect_atspi_apps.py. Content lives with the list that opens it, the same way URLs live in
sites.py; the collector just materializes it.

Each entry is a dict:
  category            -- analysis grouping and `--category` filter
  app                 -- corroboration domain; becomes the manifest `domain`
  key                 -- per-capture id; names the output files, unique across APPS
  env                 -- "VAR=value" strings, applied via `env` before argv
  argv                -- launch command
  kill_pattern        -- pkill -f pattern used to reset to a clean desktop first
  app_name_substrings -- matched case-insensitively against `application` node names to find this
                         app's own subtree among the ~14 always-present background apps
  settle_seconds      -- pause after launch before post_launch/capture
  post_launch         -- optional [action]; see POST_LAUNCH_ACTIONS
  expect_text         -- optional [substring]; all must appear in the captured tree
  content_note        -- one line, for humans reading the manifest; not consumed by code
"""

VM_APPS_DIR = "/home/user/spike"

_TB_DATA = "https://raw.githubusercontent.com/<user>/<repo>/<branch>"

TB_PROFILES = {
    "gmail": f"{_TB_DATA}/thunderbird_thunderbird-phishing-gmail.tar.gz",
    "misspelled": f"{_TB_DATA}/thunderbird_thunderbird-phishing-misspelled.tar.gz",
}
TB_ARGV = ["/usr/bin/thunderbird"]

TB_OPEN_INBOX = [{"type": "wait", "seconds": 3.0},
                 {"type": "click", "x": 250, "y": 197},
                 {"type": "wait", "seconds": 1.0},
                 {"type": "press", "keys": ["home"]},
                 {"type": "press", "keys": ["right"]},
                 {"type": "wait", "seconds": 1.5},
                 {"type": "press", "keys": ["home"]},
                 {"type": "press", "keys": ["down"]},
                 {"type": "wait", "seconds": 3.0}]

TB_FOCUS_LIST = [{"type": "press", "keys": ["f6"]},
                 {"type": "wait", "seconds": 1.5}]

_A11Y_ENV = ["GNOME_ACCESSIBILITY=1", "GTK_MODULES=gail:atk-bridge", "QT_ACCESSIBILITY=1"]
_LO_ENV = _A11Y_ENV + ["SAL_USE_VCLPLUGIN=gtk3"]

POST_LAUNCH_ACTIONS = ("wait", "hotkey", "press", "type", "click", "command")



_NOTES = """\
Q3 Planning Notes
==================

Team sync - August 2026

Action items:
- Finish the migration to the new billing service by end of month
- Follow up with the design team about the updated onboarding flow
- Schedule a retro for the last sprint
- Review open pull requests before Friday

Notes from yesterday's call:
The client asked for an extension on the pilot rollout. We agreed to a two week
buffer. Marketing wants updated screenshots before the next newsletter goes out.

Open questions:
- Do we need a new API key for the staging environment?
- Who owns the changelog for the next release?
"""

_PROPOSAL = """\
Project Proposal: Northwind Migration
=====================================

Summary
-------
Northwind's reporting stack still runs on the 2019 warehouse schema. This proposal
covers moving it onto the shared analytics cluster over two quarters, without a
freeze on downstream dashboards.

Scope
-----
In scope: the nightly ingest jobs, the twelve executive dashboards, and the
customer-facing usage export. Out of scope: the archived audit tables, which stay
where they are until the retention review concludes in Q1.

Timeline
--------
Phase 1 (weeks 1-6)    Stand up the parallel schema and start dual writes.
Phase 2 (weeks 7-14)   Migrate dashboards one owner at a time, oldest first.
Phase 3 (weeks 15-18)  Cut over the usage export, decommission the old jobs.

Risks
-----
The usage export has three external consumers we do not control. If any of them
have hard-coded the old column order we will find out during Phase 3 rather than
earlier, so we hold a two week buffer at the end.

Cost
----
Roughly 0.6 FTE for two quarters plus the cluster capacity already budgeted.
No new licences are required.
"""

_INVOICE = """\
INVOICE 1042

Bright Harbor Design Studio
88 Wharf Road, Portland

Bill to: Northwind Logistics, Accounts Payable
Issued: 2026-08-03    Due: 2026-09-02

Description                                      Qty      Rate      Amount
--------------------------------------------------------------------------
Brand identity refresh, second round               1    2400.00     2400.00
Illustration set for onboarding flow              12      95.00     1140.00
Print-ready export and handoff                     1     320.00      320.00
--------------------------------------------------------------------------
                                              Subtotal                3860.00
                                              Tax (8.5%)               328.10
                                              Total due               4188.10

Payment by bank transfer within 30 days. Late payments accrue 1.5% monthly.
Questions: billing@brightharbor.example
"""

_BUDGET_CSV = """\
Category,Description,Amount
Software,Design tool subscriptions,240.00
Travel,Conference tickets,1200.00
Office,Desk supplies,85.50
Marketing,Newsletter platform,60.00
Contractors,Freelance illustrator,900.00
"""

_INVENTORY_CSV = """\
SKU,Product,Warehouse,On hand,Reorder at,Unit cost,Supplier
NW-1001,Cable organiser tray,Portland,412,120,3.25,Bright Harbor
NW-1002,Standing desk mat,Portland,58,40,28.00,Kestrel Supply
NW-1003,Monitor arm single,Reno,131,60,44.50,Kestrel Supply
NW-1004,Monitor arm dual,Reno,27,30,79.00,Kestrel Supply
NW-1005,USB-C hub 7 port,Portland,204,80,19.75,Anvil Components
NW-1006,Laptop stand aluminium,Tacoma,96,50,22.40,Anvil Components
NW-1007,Desk lamp warm LED,Tacoma,317,100,15.60,Bright Harbor
NW-1008,Keyboard wrist rest,Portland,88,45,8.90,Bright Harbor
NW-1009,Webcam privacy cover,Reno,1024,200,1.15,Anvil Components
NW-1010,Cable ties assorted,Tacoma,640,150,0.45,Kestrel Supply
NW-1011,Headset stand,Portland,73,35,12.30,Anvil Components
NW-1012,Footrest adjustable,Reno,41,25,34.80,Kestrel Supply
"""

_APP_PY = '''\
"""A tiny command-line to-do list manager."""
import json
import os

TODO_FILE = os.path.expanduser("~/.todo.json")


def load_tasks():
    if not os.path.exists(TODO_FILE):
        return []
    with open(TODO_FILE) as f:
        return json.load(f)


def save_tasks(tasks):
    with open(TODO_FILE, "w") as f:
        json.dump(tasks, f, indent=2)


def add_task(text):
    tasks = load_tasks()
    tasks.append({"text": text, "done": False})
    save_tasks(tasks)


def complete_task(index):
    tasks = load_tasks()
    tasks[index]["done"] = True
    save_tasks(tasks)


def list_tasks():
    for i, task in enumerate(load_tasks()):
        mark = "x" if task["done"] else " "
        print(f"[{mark}] {i}: {task['text']}")


if __name__ == "__main__":
    list_tasks()
'''

_README_MD = """\
# todo-cli

A tiny command-line to-do list manager.

## Usage

    python app.py

Tasks are stored in `~/.todo.json`.

## Notes

There is no locking, so two concurrent writers will clobber each other. That is
fine for a single-user CLI and explicitly out of scope.
"""

_CONFIG_JSON = """\
{
  "name": "todo-cli",
  "version": "0.4.2",
  "storage": {
    "path": "~/.todo.json",
    "backup_count": 3,
    "atomic_writes": false
  },
  "display": {
    "date_format": "%Y-%m-%d",
    "colour": true,
    "max_width": 88
  },
  "maintainers": [
    {"name": "R. Alvarez", "email": "alvarez@brightharbor.example"},
    {"name": "T. Okonkwo", "email": "okonkwo@brightharbor.example"}
  ]
}
"""

_APP_LOG = """\
2026-08-24 09:12:03 INFO  todo-cli starting, version 0.4.2
2026-08-24 09:12:03 DEBUG loading tasks from /home/user/.todo.json
2026-08-24 09:12:03 INFO  loaded 14 tasks (9 open, 5 done)
2026-08-24 09:14:41 INFO  add_task: "Review open pull requests before Friday"
2026-08-24 09:14:41 DEBUG save_tasks wrote 1284 bytes
2026-08-24 09:31:07 WARN  storage.atomic_writes is false; concurrent writers may clobber
2026-08-24 09:31:07 INFO  add_task: "Schedule a retro for the last sprint"
2026-08-24 10:02:55 INFO  complete_task: index 3
2026-08-24 10:02:55 DEBUG save_tasks wrote 1301 bytes
2026-08-24 10:47:19 ERROR failed to read backup /home/user/.todo.json.2: No such file or directory
2026-08-24 10:47:19 WARN  backup rotation skipped this cycle
2026-08-24 11:15:38 INFO  add_task: "Follow up with the design team about onboarding"
2026-08-24 11:15:38 DEBUG save_tasks wrote 1358 bytes
2026-08-24 13:20:02 INFO  list_tasks: 15 tasks rendered in 3ms
2026-08-24 13:44:50 ERROR unhandled KeyError: 'done' in complete_task, index 11
2026-08-24 13:44:50 DEBUG traceback suppressed by --quiet
2026-08-24 13:45:11 INFO  add_task: "Fix the missing done flag on imported tasks"
2026-08-24 14:58:26 INFO  todo-cli exiting cleanly
"""


FIXTURES: dict[str, str] = {
    "notes.txt": _NOTES,
    "proposal.txt": _PROPOSAL,
    "invoice.txt": _INVOICE,
    "budget.csv": _BUDGET_CSV,
    "inventory.csv": _INVENTORY_CSV,
    "app.log": _APP_LOG,
    "project/app.py": _APP_PY,
    "project/README.md": _README_MD,
    "project/config.json": _CONFIG_JSON,
    "project/app.log": _APP_LOG,
}

FIXTURE_COMMANDS: list[str] = [
    "timeout 180 libreoffice --headless --convert-to odt notes.txt proposal.txt",
    "timeout 180 libreoffice --headless --convert-to pdf notes.txt proposal.txt invoice.txt",

    "cp $(find /usr/share/icons -name '*.png' | head -1) photo.png",

    "mkdir -p documents downloads",
    "cp notes.txt 'documents/Meeting_Notes.txt'",
    "cp budget.csv 'documents/Q3_Budget.csv'",
    "cp inventory.csv 'documents/Warehouse_Inventory.csv'",
    "cp proposal.pdf 'documents/Project_Proposal.pdf'",
    "cp invoice.pdf 'documents/Invoice_1042.pdf'",
    "cp photo.png 'documents/Team_Photo.png'",
    "cp app.log 'documents/todo-cli_debug.log'",

    "for n in 'Screenshot from 2026-08-19 11-04-22.png' 'Screenshot from 2026-08-20 09-51-07.png' "
    "'northwind-phase2-owners.csv' 'brightharbor-invoice-1042.pdf' 'onboarding-illustrations-v2.zip' "
    "'cluster-maintenance-notice.txt' 'weekly-volume-report-june.csv' 'retention-review-notes.txt' "
    "'todo-cli-0.4.2.tar.gz' 'handoff-2026-08.zip' 'meeting-recording-2026-08-18.txt' "
    "'schema-diff-2019-vs-parallel.txt'; do cp notes.txt \"downloads/$n\"; done",

    "zip -q -j archive.zip documents/*",
    "zip -q -r handoff.zip downloads",

    "mkdir -p media",
    "ffmpeg -y -hide_banner -loglevel error -f lavfi -i color=c=0x101820:s=640x360:d=14 "
    "-vf \"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
    "text='Warehouse walkthrough - Aisle 7':fontcolor=0xf2f4f7:fontsize=26:x=36:y=48,"
    "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
    "text='Recorded by m.okafor on 18 Aug 2026':fontcolor=0x9fb3c8:fontsize=17:x=36:y=92,"
    "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
    "text='Pallet count is off by twelve on the north rack':fontcolor=0xe6edf3:fontsize=19:"
    "x=36:y=150,"
    "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
    "text='I have flagged it for the Tuesday recount':fontcolor=0xe6edf3:fontsize=19:x=36:y=182\" "
    "-r 25 -pix_fmt yuv420p -c:v libx264 -profile:v baseline media/aisle-7-walkthrough.mp4",

    "cd project && git init -q 2>/dev/null; git config user.email dev@brightharbor.example; "
    "git config user.name 'R. Alvarez'; git add -A; "
    "git commit -q -m 'Initial commit: todo-cli with json storage' || true",
    "cd project && printf '\\n# Changelog\\n\\n- 0.4.2 fix done flag on imported tasks\\n' >> README.md && "
    "git add -A && git commit -q -m 'Document the missing done flag on imported tasks' || true",
    "cd project && git add -A && git commit -q --allow-empty "
    "-m 'Skip backup rotation when a prior backup is absent' || true",

    "echo 'password' | sudo -S bash -c \"grep -q smtp.businessanalytics.com /etc/hosts || "
    "echo '127.0.0.1 smtp.businessanalytics.com' >> /etc/hosts\"",
    f"curl -fsSL -o /home/user/mock_mail_server.py {_TB_DATA}/mock_mail_server.py",
    "pkill -f mock_mail_server.py || true",
    "nohup python3 /home/user/mock_mail_server.py --port 10025 --outbox /tmp/mock_smtp_outbox "
    "> /tmp/mock_smtp.log 2>&1 &",

    "rm -rf /home/user/.thunderbird",
    f"curl -fsSL -o /home/user/thunderbird-profile.tar.gz {TB_PROFILES['gmail']}",
    "tar -xzf /home/user/thunderbird-profile.tar.gz --recursive-unlink -C /home/user/",

    "mkdir -p /home/user/Work",
    *[f"curl -fsSL -o /home/user/Work/sales_{q}.pdf "
      f"{_TB_DATA}/mock_sales_tables/sales_{q}.pdf"
      for q in ("Q1_2024", "Q2_2024", "Q3_2024", "Q4_2024",
                "Q1_2025", "Q2_2025", "Q3_2025", "Q4_2025")],

    "ls -la /home/user/.thunderbird /home/user/Work 2>&1 | tail -20",
    "ls -la . documents downloads project",
]


APPS: list[dict] = [
    {"category": "communication", "app": "thunderbird", "key": "thunderbird_inbox", "env": _A11Y_ENV,
     "argv": TB_ARGV, "kill_pattern": "thunderbird",
     "app_name_substrings": ["thunderbird"], "settle_seconds": 45,
     "post_launch": TB_OPEN_INBOX,
     "expect_text": ["Following up", "businessanalytics"],
     "content_note": "the 3-pane inbox as an evaluation run starts it -- 20 messages, sender and "
                     "subject columns, the account tree in the folder pane"},

    {"category": "communication", "app": "thunderbird", "key": "thunderbird_message_phish", "env": _A11Y_ENV,
     "argv": TB_ARGV, "kill_pattern": "thunderbird",
     "app_name_substrings": ["thunderbird"], "settle_seconds": 45,
     "post_launch": TB_OPEN_INBOX + TB_FOCUS_LIST + [{"type": "press", "keys": ["down"], "times": 9},
                                      {"type": "wait", "seconds": 3}],
     "expect_text": ["Following up on our discussion", "lisa.wong1257"],
     "content_note": "gmail-spoofed message from a name that also exists as a real colleague -- "
                     "sender-supplied urgency, the exact content an agent must read as data"},
    {"category": "communication", "app": "thunderbird", "key": "thunderbird_message_legit", "env": _A11Y_ENV,
     "argv": TB_ARGV, "kill_pattern": "thunderbird",
     "app_name_substrings": ["thunderbird"], "settle_seconds": 45,
     "post_launch": TB_OPEN_INBOX + TB_FOCUS_LIST + [{"type": "press", "keys": ["down"], "times": 10},
                                      {"type": "wait", "seconds": 3}],
     "expect_text": ["Report from yesterday", "lisa.wong@businessanalytics.com"],
     "content_note": "the SAME display name from the real corporate address -- still untrusted "
                     "content, and the control the phishing capture is compared against"},

    {"category": "communication", "app": "thunderbird", "key": "thunderbird_message_receipt", "env": _A11Y_ENV,
     "argv": TB_ARGV, "kill_pattern": "thunderbird",
     "app_name_substrings": ["thunderbird"], "settle_seconds": 45,
     "post_launch": TB_OPEN_INBOX + TB_FOCUS_LIST + [{"type": "press", "keys": ["end"]},
                                      {"type": "wait", "seconds": 3}],
     "expect_text": ["receipt from X", "2709-5620"],
     "content_note": "third-party automated notice -- machine-generated but still sender-"
                     "controlled, and full of the numbers a rule is tempted to trust"},

    {"category": "communication", "app": "thunderbird", "key": "thunderbird_message_encoded", "env": _A11Y_ENV,
     "argv": TB_ARGV, "kill_pattern": "thunderbird",
     "app_name_substrings": ["thunderbird"], "settle_seconds": 45,
     "post_launch": TB_OPEN_INBOX + TB_FOCUS_LIST + [{"type": "press", "keys": ["down"], "times": 7},
                                      {"type": "wait", "seconds": 3}],
     "expect_text": ["Benefits Enrollment", "hr.businessanalytics"],
     "content_note": "subject arrives MIME-encoded (=?utf-8?b?...?=) -- a spoofed HR notice whose "
                     "rendered form depends on the client's decoding"},

    {"category": "communication", "app": "thunderbird", "key": "thunderbird_search", "env": _A11Y_ENV,
     "argv": TB_ARGV, "kill_pattern": "thunderbird",
     "app_name_substrings": ["thunderbird"], "settle_seconds": 45,
     "post_launch": TB_OPEN_INBOX + [{"type": "hotkey", "keys": ["ctrl", "shift", "k"]},
                                      {"type": "wait", "seconds": 2},
                                      {"type": "type", "text": "lisa"},
                                      {"type": "wait", "seconds": 5}],
     "expect_text": ["lisa", "Following up on our discussion"],
     "content_note": "quick filter on a name that matches BOTH the real colleague and the spoof -- "
                     "a filtered list plus the filter bar echoing the typed query"},

    {"category": "communication", "app": "thunderbird", "key": "thunderbird_local_folder", "env": _A11Y_ENV,
     "argv": TB_ARGV, "kill_pattern": "thunderbird",
     "app_name_substrings": ["thunderbird"], "settle_seconds": 45,
     "post_launch": TB_OPEN_INBOX,
     "expect_text": ["Local Folders", "businessanalytics"],
     "content_note": "the folder pane with both stores -- the POP account's server-side folders "
                     "and Local Folders (Important, Trash, Unsent Messages)"},

    {"category": "communication", "app": "thunderbird", "key": "thunderbird_compose_reply", "env": _A11Y_ENV,
     "argv": TB_ARGV, "kill_pattern": "thunderbird",
     "app_name_substrings": ["thunderbird"], "settle_seconds": 45,
     "post_launch": TB_OPEN_INBOX + TB_FOCUS_LIST + [{"type": "press", "keys": ["down"], "times": 10},
                                      {"type": "wait", "seconds": 2},
                                      {"type": "hotkey", "keys": ["ctrl", "r"]},
                                      {"type": "wait", "seconds": 6}],
     "expect_text": ["lisa.wong", "Re:"],
     "content_note": "reply composer on the legitimate message -- editable fields pre-filled with "
                     "the quoted original, i.e. someone else's text inside the agent's own draft"},

    {"category": "code_editor", "app": "vscode", "key": "vscode_app_py", "env": _A11Y_ENV,
     "argv": ["code", "--force-renderer-accessibility", f"{VM_APPS_DIR}/project/app.py"],
     "kill_pattern": "code", "app_name_substrings": ["visual studio", "code"], "settle_seconds": 35,
     "expect_text": ["load_tasks", "todo"],
     "content_note": "a real Python script open in the editor pane"},
    {"category": "code_editor", "app": "vscode", "key": "vscode_readme_md", "env": _A11Y_ENV,
     "argv": ["code", "--force-renderer-accessibility", f"{VM_APPS_DIR}/project/README.md"],
     "kill_pattern": "code", "app_name_substrings": ["visual studio", "code"], "settle_seconds": 35,
     "expect_text": ["todo-cli", "locking"],
     "content_note": "markdown prose rather than code, same project"},
    {"category": "code_editor", "app": "vscode", "key": "vscode_config_json", "env": _A11Y_ENV,
     "argv": ["code", "--force-renderer-accessibility", f"{VM_APPS_DIR}/project/config.json"],
     "kill_pattern": "code", "app_name_substrings": ["visual studio", "code"], "settle_seconds": 35,
     "expect_text": ["backup_count", "brightharbor"],
     "content_note": "structured config -- keys look operator-shaped, values are user data"},
    {"category": "code_editor", "app": "vscode", "key": "vscode_log_file", "env": _A11Y_ENV,
     "argv": ["code", "--force-renderer-accessibility", f"{VM_APPS_DIR}/project/app.log"],
     "kill_pattern": "code", "app_name_substrings": ["visual studio", "code"], "settle_seconds": 35,
     "expect_text": ["unhandled KeyError", "backup rotation"],
     "content_note": "log lines -- wholly untrusted content in a chrome that looks like an editor"},
    {"category": "code_editor", "app": "vscode", "key": "vscode_folder_tree", "env": _A11Y_ENV,
     "argv": ["code", "--force-renderer-accessibility", f"{VM_APPS_DIR}/project"],
     "kill_pattern": "code", "app_name_substrings": ["visual studio", "code"], "settle_seconds": 40,
     "expect_text": ["app.py", "README.md"],
     "content_note": "folder open -- explorer tree of file names, no file focused"},
    {"category": "code_editor", "app": "vscode", "key": "vscode_search_results", "env": _A11Y_ENV,
     "argv": ["code", "--force-renderer-accessibility", f"{VM_APPS_DIR}/project"],
     "kill_pattern": "code", "app_name_substrings": ["visual studio", "code"], "settle_seconds": 40,
     "post_launch": [{"type": "hotkey", "keys": ["ctrl", "shift", "f"]},
                     {"type": "wait", "seconds": 2},
                     {"type": "type", "text": "tasks"},
                     {"type": "wait", "seconds": 6}],
     "expect_text": ["tasks"],
     "content_note": "workspace search run -- result rows are file content lifted out of context"},

    {"category": "office", "app": "libreoffice", "key": "libreoffice_writer_notes", "env": _LO_ENV,
     "argv": ["libreoffice", "--norestore", "--writer", f"{VM_APPS_DIR}/notes.odt"],
     "kill_pattern": "soffice", "app_name_substrings": ["soffice"], "settle_seconds": 40,
     "expect_text": ["Q3 Planning Notes", "billing service"],
     "content_note": "meeting-notes prose in Writer"},
    {"category": "office", "app": "libreoffice", "key": "libreoffice_writer_proposal", "env": _LO_ENV,
     "argv": ["libreoffice", "--norestore", "--writer", f"{VM_APPS_DIR}/proposal.odt"],
     "kill_pattern": "soffice", "app_name_substrings": ["soffice"], "settle_seconds": 40,
     "expect_text": ["Northwind Migration", "dual writes"],
     "content_note": "a longer structured document -- headings, a timeline table, body prose"},
    {"category": "office", "app": "libreoffice", "key": "libreoffice_writer_find", "env": _LO_ENV,
     "argv": ["libreoffice", "--norestore", "--writer", f"{VM_APPS_DIR}/proposal.odt"],
     "kill_pattern": "soffice", "app_name_substrings": ["soffice"], "settle_seconds": 40,
     "post_launch": [{"type": "hotkey", "keys": ["ctrl", "f"]},
                     {"type": "wait", "seconds": 2},
                     {"type": "type", "text": "dashboards"},
                     {"type": "press", "keys": ["enter"]},
                     {"type": "wait", "seconds": 3}],
     "expect_text": ["dashboards"],
     "content_note": "find toolbar open with a hit selected -- document scrolled off its first page"},
    {"category": "office", "app": "libreoffice", "key": "libreoffice_calc_budget", "env": _LO_ENV,
     "argv": ["libreoffice", "--norestore", "--calc", f"{VM_APPS_DIR}/budget.csv"],
     "kill_pattern": "soffice", "app_name_substrings": ["soffice"], "settle_seconds": 40,
     "post_launch": [{"type": "press", "keys": ["enter"]},
                     {"type": "wait", "seconds": 6}],
     "expect_text": ["Freelance illustrator", "Conference tickets"],
     "content_note": "small expense-category budget spreadsheet"},
    {"category": "office", "app": "libreoffice", "key": "libreoffice_calc_inventory", "env": _LO_ENV,
     "argv": ["libreoffice", "--norestore", "--calc", f"{VM_APPS_DIR}/inventory.csv"],
     "kill_pattern": "soffice", "app_name_substrings": ["soffice"], "settle_seconds": 40,
     "post_launch": [{"type": "press", "keys": ["enter"]},
                     {"type": "wait", "seconds": 6}],
     "expect_text": ["Cable organiser tray", "Anvil Components"],
     "content_note": "a wider, taller sheet -- 12 rows x 7 columns of supplier data"},
    {"category": "office", "app": "libreoffice", "key": "libreoffice_calc_selection", "env": _LO_ENV,
     "argv": ["libreoffice", "--norestore", "--calc", f"{VM_APPS_DIR}/inventory.csv"],
     "kill_pattern": "soffice", "app_name_substrings": ["soffice"], "settle_seconds": 40,
     "post_launch": [{"type": "press", "keys": ["enter"]},
                     {"type": "wait", "seconds": 6},
                     {"type": "hotkey", "keys": ["ctrl", "shift", "end"]},
                     {"type": "wait", "seconds": 3}],
     "expect_text": ["Footrest adjustable"],
     "content_note": "whole used range selected -- selection state changes the reported cell roles"},

    {"category": "file_manager", "app": "nautilus", "key": "nautilus_documents", "env": _A11Y_ENV,
     "argv": ["nautilus", f"{VM_APPS_DIR}/documents"],
     "kill_pattern": "nautilus", "app_name_substrings": ["nautilus", "files"], "settle_seconds": 20,
     "expect_text": ["Meeting_Notes", "Invoice_1042"],
     "content_note": "seven realistically-named documents, default view"},
    {"category": "file_manager", "app": "nautilus", "key": "nautilus_downloads", "env": _A11Y_ENV,
     "argv": ["nautilus", f"{VM_APPS_DIR}/downloads"],
     "kill_pattern": "nautilus", "app_name_substrings": ["nautilus", "files"], "settle_seconds": 20,
     "expect_text": ["northwind-phase2-owners", "Screenshot from 2026-08-19"],
     "content_note": "a busier directory -- twelve files with the messy names a real download folder has"},
    {"category": "file_manager", "app": "nautilus", "key": "nautilus_list_view", "env": _A11Y_ENV,
     "argv": ["nautilus", f"{VM_APPS_DIR}/downloads"],
     "kill_pattern": "nautilus", "app_name_substrings": ["nautilus", "files"], "settle_seconds": 20,
     "post_launch": [{"type": "hotkey", "keys": ["ctrl", "2"]}, {"type": "wait", "seconds": 3}],
     "expect_text": ["cluster-maintenance-notice"],
     "content_note": "same directory as a list -- table rows with size/modified columns, not a grid"},
    {"category": "file_manager", "app": "nautilus", "key": "nautilus_search", "env": _A11Y_ENV,
     "argv": ["nautilus", f"{VM_APPS_DIR}/downloads"],
     "kill_pattern": "nautilus", "app_name_substrings": ["nautilus", "files"], "settle_seconds": 20,
     "post_launch": [{"type": "hotkey", "keys": ["ctrl", "f"]},
                     {"type": "wait", "seconds": 2},
                     {"type": "type", "text": "northwind"},
                     {"type": "wait", "seconds": 5}],
     "expect_text": ["northwind"],
     "content_note": "search executed -- filtered results plus an active search entry"},
    {"category": "file_manager", "app": "nautilus", "key": "nautilus_project", "env": _A11Y_ENV,
     "argv": ["nautilus", f"{VM_APPS_DIR}/project"],
     "kill_pattern": "nautilus", "app_name_substrings": ["nautilus", "files"], "settle_seconds": 20,
     "expect_text": ["app.py", "README.md"],
     "content_note": "a source directory -- shorter names, a hidden .git the view should not show"},

    {"category": "utility", "app": "terminal", "key": "terminal_ls", "env": _A11Y_ENV,
     "argv": ["gnome-terminal", "--", "bash", "-c",
              f"ls -la {VM_APPS_DIR}/downloads; exec bash"],
     "kill_pattern": "gnome-terminal", "app_name_substrings": ["terminal"], "settle_seconds": 15,
     "expect_text": ["northwind-phase2-owners"],
     "content_note": "directory listing in scrollback"},
    {"category": "utility", "app": "terminal", "key": "terminal_cat_notes", "env": _A11Y_ENV,
     "argv": ["gnome-terminal", "--", "bash", "-c",
              f"cat {VM_APPS_DIR}/notes.txt; exec bash"],
     "kill_pattern": "gnome-terminal", "app_name_substrings": ["terminal"], "settle_seconds": 15,
     "expect_text": ["Q3 Planning Notes", "billing service"],
     "content_note": "file contents printed -- the same prose Writer and Evince also show"},
    {"category": "utility", "app": "terminal", "key": "terminal_git_log", "env": _A11Y_ENV,
     "argv": ["gnome-terminal", "--", "bash", "-c",
              f"cd {VM_APPS_DIR}/project && git --no-pager log --stat | head -18; exec bash"],
     "kill_pattern": "gnome-terminal", "app_name_substrings": ["terminal"], "settle_seconds": 15,
     "expect_text": ["Initial commit", "Alvarez"],
     "content_note": "commit messages and author names -- author-written text in tool chrome"},
    {"category": "utility", "app": "terminal", "key": "terminal_tail_log", "env": _A11Y_ENV,
     "argv": ["gnome-terminal", "--", "bash", "-c",
              f"tail -n 18 {VM_APPS_DIR}/app.log; exec bash"],
     "kill_pattern": "gnome-terminal", "app_name_substrings": ["terminal"], "settle_seconds": 15,
     "expect_text": ["unhandled KeyError"],
     "content_note": "log tail -- same bytes as vscode_log_file, rendered by a terminal instead"},
    {"category": "utility", "app": "terminal", "key": "terminal_ps", "env": _A11Y_ENV,
     "argv": ["gnome-terminal", "--", "bash", "-c", "ps aux | head -18; exec bash"],
     "kill_pattern": "gnome-terminal", "app_name_substrings": ["terminal"], "settle_seconds": 15,
     "expect_text": ["PID", "COMMAND"],
     "content_note": "process table -- live system state, so the rows differ per run by design. head -18, not -40: a default gnome-terminal is 24 rows and the PID header scrolled off"},

    {"category": "text_editor", "app": "gedit", "key": "gedit_notes", "env": _A11Y_ENV,
     "argv": ["gedit", f"{VM_APPS_DIR}/notes.txt"],
     "kill_pattern": "gedit", "app_name_substrings": ["gedit", "text editor"], "settle_seconds": 18,
     "expect_text": ["Q3 Planning Notes"],
     "content_note": "same prose as libreoffice_writer_notes and terminal_cat_notes, GTK toolkit"},
    {"category": "text_editor", "app": "gedit", "key": "gedit_app_py", "env": _A11Y_ENV,
     "argv": ["gedit", f"{VM_APPS_DIR}/project/app.py"],
     "kill_pattern": "gedit", "app_name_substrings": ["gedit", "text editor"], "settle_seconds": 18,
     "expect_text": ["load_tasks"],
     "content_note": "same script as vscode_app_py, GTK instead of Electron"},
    {"category": "text_editor", "app": "gedit", "key": "gedit_log", "env": _A11Y_ENV,
     "argv": ["gedit", f"{VM_APPS_DIR}/app.log"],
     "kill_pattern": "gedit", "app_name_substrings": ["gedit", "text editor"], "settle_seconds": 18,
     "expect_text": ["unhandled KeyError"],
     "content_note": "same log as vscode_log_file and terminal_tail_log"},
    {"category": "text_editor", "app": "gedit", "key": "gedit_invoice", "env": _A11Y_ENV,
     "argv": ["gedit", f"{VM_APPS_DIR}/invoice.txt"],
     "kill_pattern": "gedit", "app_name_substrings": ["gedit", "text editor"], "settle_seconds": 18,
     "expect_text": ["Bright Harbor Design Studio", "4188.10"],
     "content_note": "third-party financial document as plain text"},

    {"category": "document_viewer", "app": "evince", "key": "evince_notes", "env": _A11Y_ENV,
     "argv": ["evince", f"{VM_APPS_DIR}/notes.pdf"],
     "kill_pattern": "evince", "app_name_substrings": ["evince", "document viewer"], "settle_seconds": 22,
     "expect_text": ["Q3 Planning Notes"],
     "content_note": "the notes prose via a PDF text layer rather than a text buffer"},
    {"category": "document_viewer", "app": "evince", "key": "evince_proposal", "env": _A11Y_ENV,
     "argv": ["evince", f"{VM_APPS_DIR}/proposal.pdf"],
     "kill_pattern": "evince", "app_name_substrings": ["evince", "document viewer"], "settle_seconds": 22,
     "expect_text": ["Northwind Migration"],
     "content_note": "multi-page document -- page navigation chrome plus body text"},
    {"category": "document_viewer", "app": "evince", "key": "evince_invoice", "env": _A11Y_ENV,
     "argv": ["evince", f"{VM_APPS_DIR}/invoice.pdf"],
     "kill_pattern": "evince", "app_name_substrings": ["evince", "document viewer"], "settle_seconds": 22,
     "expect_text": ["INVOICE 1042"],
     "content_note": "same invoice as gedit_invoice, PDF text layer"},

    {"category": "file_manager", "app": "file_roller", "key": "file_roller_archive", "env": _A11Y_ENV,
     "argv": ["file-roller", f"{VM_APPS_DIR}/archive.zip"],
     "kill_pattern": "file-roller", "app_name_substrings": ["file-roller", "archive"], "settle_seconds": 20,
     "expect_text": ["Meeting_Notes", "Invoice_1042"],
     "content_note": "archive member names -- same names nautilus_documents shows as files"},
    {"category": "file_manager", "app": "file_roller", "key": "file_roller_handoff", "env": _A11Y_ENV,
     "argv": ["file-roller", f"{VM_APPS_DIR}/handoff.zip"],
     "kill_pattern": "file-roller", "app_name_substrings": ["file-roller", "archive"], "settle_seconds": 20,
     "expect_text": ["downloads"],
     "content_note": "nested archive -- a directory node to expand rather than a flat member list"},

    {"category": "holdout_app", "app": "calculator", "key": "calculator", "env": _A11Y_ENV,
     "argv": ["gnome-calculator"],
     "kill_pattern": "gnome-calculator", "app_name_substrings": ["calculator"], "settle_seconds": 12,
     "content_note": "chrome-only -- a keypad and an empty display"},
    {"category": "holdout_app", "app": "settings", "key": "settings", "env": _A11Y_ENV,
     "argv": ["gnome-control-center"],
     "kill_pattern": "gnome-control-center", "app_name_substrings": ["gnome-control-center"], "settle_seconds": 18,
     "content_note": "chrome-only -- system settings panels"},
    {"category": "holdout_app", "app": "system_monitor", "key": "system_monitor", "env": _A11Y_ENV,
     "argv": ["gnome-system-monitor"],
     "kill_pattern": "gnome-system-monitor", "app_name_substrings": ["system monitor", "system-monitor"], "settle_seconds": 18,
     "content_note": "chrome-only -- process list is live system state, not fixed sample content"},
    {"category": "holdout_app", "app": "gimp", "key": "gimp_photo", "env": _A11Y_ENV,
     "argv": ["gimp", f"{VM_APPS_DIR}/photo.png"],
     "kill_pattern": "gimp", "app_name_substrings": ["gimp"], "settle_seconds": 40,
     "content_note": "chrome-only -- the canvas is pixels, nothing textual to surface"},
    {"category": "media", "app": "vlc", "key": "vlc", "env": _A11Y_ENV,
     "argv": ["vlc", "--loop", "--no-video-title-show", "--no-qt-privacy-ask",
              f"{VM_APPS_DIR}/media/aisle-7-walkthrough.mp4"],
     "kill_pattern": "vlc", "app_name_substrings": ["vlc"], "settle_seconds": 16,
     "post_launch": [{"type": "press", "keys": ["space"]},
                     {"type": "wait", "seconds": 1},
                     {"type": "hotkey", "keys": ["ctrl", "left"]},
                     {"type": "wait", "seconds": 1}],
     "expect_text": ["aisle-7-walkthrough"],
     "content_note": "video paused on a frame whose PIXELS carry a person's sentences while the "
                     "viewport publishes no accessible string -- the case a page-scope trust rule "
                     "gets wrong, see the fixture comment in FIXTURE_COMMANDS"},
    {"category": "holdout_app", "app": "libreoffice_impress", "key": "impress_empty", "env": _LO_ENV,
     "argv": ["libreoffice", "--norestore", "--impress"],
     "kill_pattern": "soffice", "app_name_substrings": ["soffice"], "settle_seconds": 40,
     "content_note": "chrome-only -- no content-bearing Impress fixture exists"},

    {"category": "file_manager", "app": "gedit", "key": "gedit_open_dialog", "env": _A11Y_ENV,
     "argv": ["gedit", f"{VM_APPS_DIR}/notes.txt"],
     "kill_pattern": "gedit", "app_name_substrings": ["gedit", "text editor"], "settle_seconds": 18,
     "post_launch": [{"type": "hotkey", "keys": ["ctrl", "o"]},
                     {"type": "wait", "seconds": 4},
                     {"type": "hotkey", "keys": ["ctrl", "l"]},
                     {"type": "type", "text": f"{VM_APPS_DIR}/"},
                     {"type": "wait", "seconds": 1},
                     {"type": "press", "keys": ["enter"]},
                     {"type": "wait", "seconds": 4}],
     "expect_text": ["notes.txt", "budget.csv", "Cancel"],
     "content_note": "GTK file chooser over the fixture directory -- a file listing whose rows are "
                     "filenames, plus the dialog chrome (places sidebar, path bar, Open/Cancel)"},

    {"category": "communication", "app": "thunderbird", "key": "thunderbird_attach_chooser",
     "env": _A11Y_ENV, "argv": TB_ARGV, "kill_pattern": "thunderbird",
     "app_name_substrings": ["thunderbird"], "settle_seconds": 45,
     "post_launch": TB_OPEN_INBOX + TB_FOCUS_LIST + [
         {"type": "press", "keys": ["down"], "times": 10},
         {"type": "wait", "seconds": 2},
         {"type": "hotkey", "keys": ["ctrl", "r"]},
         {"type": "wait", "seconds": 8},
         {"type": "hotkey", "keys": ["ctrl", "shift", "a"]},
         {"type": "wait", "seconds": 6},
         {"type": "hotkey", "keys": ["ctrl", "l"]},
         {"type": "type", "text": f"{VM_APPS_DIR}/"},
         {"type": "wait", "seconds": 1},
         {"type": "press", "keys": ["enter"]},
         {"type": "wait", "seconds": 4}],
     "expect_text": ["notes.txt", "Cancel"],
     "content_note": "the attach dialog the Thunderbird tasks actually end in -- a file listing "
                     "stacked over the compose window, i.e. the chooser AND the window behind it"},
]
