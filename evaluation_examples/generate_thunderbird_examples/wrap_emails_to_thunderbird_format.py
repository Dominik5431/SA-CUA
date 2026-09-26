#!/usr/bin/env python3
"""
Create a Thunderbird POP3 maildir profile by cloning a working template,
swapping in account settings and emails from a JSON file, then packing
the result as a .tar.gz ready for OSWorld VMs.
"""
import json
import mimetypes
import os
import shutil
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

_ACCOUNT_EMAIL   = "john.smith@businessanalytics.com"
_FULL_NAME       = "John Smith"
_POP_HOST        = "pop.businessanalytics.com"
_SMTP_HOST       = "smtp.businessanalytics.com"
_PROFILE_DIR     = "2j3k4l5m.default-release"

_VM_UTC_OFFSET_HOURS = 8

_TEMPLATE_DIR    = "lh74c3yu.default-release-1"
_TEMPLATE_EMAIL  = "mathias.gadenzi@gmail.com"
_TEMPLATE_EMAIL_ENC = "mathias.gadenzi%40gmail.com"
_TEMPLATE_NAME   = "Mathias Gadenzi"
_TEMPLATE_POP    = "pop.gmail.com"
_TEMPLATE_SMTP   = "smtp.gmail.com"

_TEMPLATE_TARBALL = os.path.normpath(os.path.join(
    os.path.dirname(__file__),
    "../../submodules/OSWorld_examples_data/template_thunderbird.tar.xz",
))

_STRIP_PREF_KEYS = [
    "calendar.caldav.googleResync",
    "mail.identity.id1.archive_folder",
    "mail.identity.id1.draft_folder",
    "mail.identity.id1.drafts_folder_picker_mode",
    "mail.identity.id1.fcc_folder",
    "mail.identity.id1.fcc_folder_picker_mode",
    "mail.identity.id1.stationery_folder",
    "mail.identity.id1.tmpl_folder_picker_mode",
    "mail.server.server1.authMethod",
    "mail.server.server1.clientid",
    "mail.server.server1.delete_by_age_from_server",
    "mail.server.server1.delete_mail_left_on_server",
    "mail.server.server1.lastFilterTime",
    "mail.server.server1.nextFilterTime",
    "mail.server.server1.num_days_to_leave_on_server",
    "mail.server.server1.oauth2.issuer",
    "mail.server.server1.oauth2.scope",
    "mail.server.server1.spamActionTargetAccount",
    "mail.server.server2.lastFilterTime",
    "mail.server.server2.nextFilterTime",
    "mail.server.server2.spamActionTargetAccount",
    "mail.smtpserver.smtp1.authMethod",
    "mail.smtpserver.smtp1.clientid",
    "mail.smtpserver.smtp1.description",
    "mail.smtpserver.smtp1.oauth2.issuer",
    "mail.smtpserver.smtp1.oauth2.scope",
]


def _resolve_timestamp(days_ago, now: int, vm_tz) -> int:
    """Return a Unix timestamp for *days_ago* relative to *now*.

    Supports the legacy form (a number of days, e.g. ``1``) and an
    arrival-time form ``"<days>:<HH>:<MM>[:<SS>]"``, e.g. ``"1:17:43"``
    meaning that many days ago at the given wall-clock time in *vm_tz*.
    """
    if isinstance(days_ago, str) and ":" in days_ago:
        parts = days_ago.split(":")
        if len(parts) not in (3, 4):
            raise ValueError(
                f"Invalid days_ago value {days_ago!r}: expected "
                "'<days>:<hour>:<minute>' or "
                "'<days>:<hour>:<minute>:<second>'")
        days = int(parts[0])
        hour = int(parts[1])
        minute = int(parts[2])
        second = int(parts[3]) if len(parts) == 4 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            raise ValueError(f"Invalid time in days_ago value {days_ago!r}")
        target = (datetime.fromtimestamp(now, tz=vm_tz) - timedelta(days=days)).replace(
            hour=hour, minute=minute, second=second, microsecond=0)
        return int(target.timestamp())

    return now - int(float(days_ago) * 86400)


def _replace_in_file(path: str, old: str, new: str) -> None:
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    content = content.replace(old, new)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def build_thunderbird_profile(
    emails_json_path: str,
    output_path: str,
    *,
    account_email: str = _ACCOUNT_EMAIL,
    full_name: str = _FULL_NAME,
    pop_host: str = _POP_HOST,
    smtp_host: str = _SMTP_HOST,
    profile_dir: str = _PROFILE_DIR,
    template_tarball: str = _TEMPLATE_TARBALL,
    vm_utc_offset_hours: int = _VM_UTC_OFFSET_HOURS,
) -> str:
    """Read *emails_json_path*, clone *template_tarball* with account
    settings replaced, insert emails, delete caches that Thunderbird
    will regenerate, and write a gzip tarball to *output_path*.

    *emails_json_path* must point to a JSON array of objects with keys
    ``sender``, ``subject``, ``body``, ``days_ago``.  An optional ``html``
    key provides an HTML version of the body; when present the message is
    written as multipart/alternative so HTML-capable clients render the
    styled version while plain-text clients fall back to ``body``.

    ``days_ago`` may be a number of days (e.g. ``1``) or an arrival time
    string (e.g. ``"1:17:43"``) meaning that many days ago at the given
    local time of day.

    Returns *output_path*.
    """
    if not os.path.exists(template_tarball):
        raise FileNotFoundError(f"Template not found: {template_tarball}")

    with open(emails_json_path, "r", encoding="utf-8") as fh:
        emails = json.load(fh)

    vm_tz = timezone(timedelta(hours=vm_utc_offset_hours))
    email_enc = account_email.replace("@", "%40")

    tmpdir = tempfile.mkdtemp()
    try:
        with tarfile.open(template_tarball, "r:xz") as tar:
            tar.extractall(tmpdir)

        tb_dir   = os.path.join(tmpdir, ".thunderbird")
        old_pdir = os.path.join(tb_dir, _TEMPLATE_DIR)
        new_pdir = os.path.join(tb_dir, profile_dir)

        os.rename(old_pdir, new_pdir)

        old_mail = os.path.join(new_pdir, "Mail", _TEMPLATE_POP)
        new_mail = os.path.join(new_pdir, "Mail", pop_host)
        os.rename(old_mail, new_mail)

        for rel in [
            os.path.join("Mail", pop_host, "Inbox.msf"),
            os.path.join("Mail", pop_host, "Trash.msf"),
            "folderCache.json",
            "folderTree.json",
            "virtualFolders.dat",
            "session.json",
            "session.json.backup",
            "sessionCheckpoints.json",
            "global-messages-db.sqlite",
            "logins.json",
            "cookies.sqlite",
            "AlternateServices.txt",
            "SiteSecurityServiceState.txt",
            "lock",
            ".parentlock",
        ]:
            p = os.path.join(new_pdir, rel)
            if os.path.lexists(p):
                os.unlink(p)

        inbox_cur = os.path.join(new_mail, "Inbox", "cur")
        for old_eml in os.listdir(inbox_cur):
            os.remove(os.path.join(inbox_cur, old_eml))

        prefs_path = os.path.join(new_pdir, "prefs.js")
        subs = [
            (_TEMPLATE_DIR,        profile_dir),
            (_TEMPLATE_EMAIL_ENC,  email_enc),
            (_TEMPLATE_EMAIL,      account_email),
            (_TEMPLATE_NAME,       full_name),
            (_TEMPLATE_POP,        pop_host),
            (_TEMPLATE_SMTP,       smtp_host),
        ]
        for old, new in subs:
            _replace_in_file(prefs_path, old, new)

        _replace_in_file(
            prefs_path,
            'user_pref("mail.smtpserver.smtp1.port", 465);',
            'user_pref("mail.smtpserver.smtp1.port", 10025);',
        )
        _replace_in_file(
            prefs_path,
            'user_pref("mail.smtpserver.smtp1.try_ssl", 3);',
            'user_pref("mail.smtpserver.smtp1.try_ssl", 0);',
        )

        with open(prefs_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        kept = [line for line in lines
                if not any(f'user_pref("{k}"' in line for k in _STRIP_PREF_KEYS)]

        kept.append('user_pref("mail.server.server1.login_at_startup", false);\n')
        kept.append('user_pref("mail.server.server1.download_on_biff", false);\n')
        kept.append('user_pref("mailnews.default_sort_type", 18);\n')
        kept.append('user_pref("mailnews.default_sort_order", 2);\n')
        kept.append('user_pref("mail.smtpserver.smtp1.authMethod", 1);\n')

        with open(prefs_path, "w", encoding="utf-8") as fh:
            fh.writelines(kept)

        _replace_in_file(os.path.join(tb_dir, "profiles.ini"), _TEMPLATE_DIR, profile_dir)
        _replace_in_file(os.path.join(tb_dir, "profiles.ini"), "Name=mathias-gadenzi", "Name=default")
        _replace_in_file(os.path.join(tb_dir, "installs.ini"),  _TEMPLATE_DIR, profile_dir)

        now = int(time.time())
        pid = os.getpid()
        for i, e in enumerate(emails, 1):
            ts = _resolve_timestamp(e.get("days_ago", 0), now, vm_tz)
            msg = EmailMessage()
            msg["From"]       = e["sender"]
            msg["To"]         = account_email
            msg["Subject"]    = e["subject"]
            msg["Date"]       = format_datetime(datetime.fromtimestamp(ts, tz=vm_tz))
            msg["Message-ID"] = make_msgid(domain=pop_host.split(".", 1)[-1])
            msg["X-Mozilla-Status"]  = "0001"
            msg["X-Mozilla-Status2"] = "00000000"
            msg.set_content(e["body"])

            html_body = e.get("html")
            if html_body:
                msg.add_alternative(html_body, subtype="html")

            for att_path in e.get("attachments", []):
                att_full = os.path.join(
                    os.path.dirname(os.path.abspath(emails_json_path)), att_path
                )
                if not os.path.exists(att_full):
                    raise FileNotFoundError(
                        f"Attachment not found: {att_full}")
                with open(att_full, "rb") as fh_att:
                    att_data = fh_att.read()
                mime_type, _ = mimetypes.guess_type(att_path)
                main_type, sub_type = (mime_type or "application/octet-stream").split("/", 1)
                msg.add_attachment(att_data, maintype=main_type,
                                   subtype=sub_type,
                                   filename=os.path.basename(att_path))

            fname = f"{ts}.{pid}_localhost.{i}:2,S"
            with open(os.path.join(inbox_cur, fname), "w") as fh:
                fh.write(msg.as_string())

        for d in [new_mail, os.path.join(new_pdir, "Mail", "Local Folders")]:
            with open(os.path.join(d, "msgFilterRules.dat"), "w") as fh:
                fh.write('version="9"\nlogging="no"\n')
        with open(os.path.join(new_mail, "popstate.dat"), "w") as fh:
            fh.write("# POP3 State File\n# Do not edit.\n\n")

        inbox_uri = f"mailbox://{email_enc}@{pop_host}/Inbox"
        session = {
            "rev": 0,
            "windows": [{
                "type": "3pane",
                "tabs": {
                    "rev": 0,
                    "selectedIndex": 0,
                    "tabs": [{
                        "mode": "mail3PaneTab",
                        "state": {
                            "firstTab": True,
                            "folderPaneVisible": True,
                            "folderURI": inbox_uri,
                            "messagePaneVisible": True,
                        },
                        "ext": {},
                    }],
                },
            }],
        }
        with open(os.path.join(new_pdir, "session.json"), "w", encoding="utf-8") as fh:
            json.dump(session, fh, separators=(",", ":"))

        with tarfile.open(output_path, "w:gz") as tar:
            tar.add(tb_dir, arcname=".thunderbird")

        return output_path
    finally:
        shutil.rmtree(tmpdir)


if __name__ == "__main__":
    _default_emails = os.path.join(
        os.path.dirname(__file__), "email_examples", "default_emails.json")
    _default_output = os.path.normpath(os.path.join(
        os.path.dirname(__file__),
        "../../submodules/OSWorld_examples_data/thunderbird-profile-pop3-maildir.tar.gz"))

    emails_json = sys.argv[1] if len(sys.argv) > 1 else _default_emails
    output      = sys.argv[2] if len(sys.argv) > 2 else _default_output

    result = build_thunderbird_profile(emails_json, output)
    print(f"Profile: {result} ({os.path.getsize(result)} bytes)")
