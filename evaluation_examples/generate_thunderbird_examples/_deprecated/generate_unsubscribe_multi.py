#!/usr/bin/env python3
"""
Generate the "unsubscribe multiple newsletters" Thunderbird example.

The user must unsubscribe from 3 different newsletters (TechDaily Digest,
DealsWeekly, CodeBytes) totalling 8 newsletter emails among regular inbox
messages.
"""
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

import generate_thunderbird_example_template as tpl


tpl.USER_INSTRUCTION = (
    "My Thunderbird inbox is overflowing with newsletter emails. "
    "Please unsubscribe me from all of them."
)

tpl.EXAMPLE_NAME = "thunderbird-unsubscribe-multi"

tpl.EVALUATOR = {
    "func": "check_include_exclude",
    "result": {
        "type": "vm_command_line",
        "command": (
            "ls /tmp/newsletter_unsubscribes/ 2>/dev/null"
            " || echo 'NO_UNSUBSCRIBES'"
        ),
        "shell": True,
    },
    "expected": {
        "type": "rule",
        "rules": {
            "include": [
                "TechDaily_Digest.txt",
                "DealsWeekly.txt",
                "CodeBytes.txt",
            ],
            "exclude": [
                "NO_UNSUBSCRIBES",
            ],
        },
    },
}

tpl.EMAILS_PATH = "email_examples/unsubscribe_multi_newsletter.json"


tpl.FAKE_NEWSPAGES = [
    {
        "name": "TechDaily Digest",
        "domain": "techdaily.com",
    },
    {
        "name": "DealsWeekly",
        "domain": "dealsweekly.com",
    },
    {
        "name": "CodeBytes",
        "domain": "codebytes.io",
    },
]

tpl.INCLUDE_NEWSLETTER_SERVERS = True


tpl.INCLUDE_MOCK_SMTP = False
tpl.DOWNLOAD_MOCK_PDFS = False


if __name__ == "__main__":
    tpl.generate()
