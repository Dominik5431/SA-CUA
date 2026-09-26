from __future__ import annotations
import xml.etree.ElementTree as ET

_ST = "https://accessibility.ubuntu.example.org/ns/state"


def get_os_state(a11y_xml: str) -> str:
    """Return a token describing the current desktop state from an AT-SPI2 XML string.

    Tokens:
        PARSE_ERROR               – XML could not be parsed
        DESKTOP_BLANK             – bare GNOME desktop (only gjs running)
        BROWSER_OPEN:<url>        – Chrome/Chromium visible with a URL
        BROWSER_MINIMISED         – Chrome running but no visible frame
        TERMINAL_OPEN             – terminal emulator open
        FILE_MANAGER_OPEN         – Nautilus/Files open
        EDITOR_OPEN:<frame-name>  – text editor open
        LIBREOFFICE_OPEN:<name>   – LibreOffice window open
        UNKNOWN:<app1,app2,...>   – unrecognised combination of apps
    """
    try:
        root = ET.fromstring(a11y_xml)
    except ET.ParseError:
        return "PARSE_ERROR"

    apps = root.findall(".//application")
    app_names = [a.get("name", "") for a in apps]

    non_shell = [n for n in app_names if n not in ("gjs", "")]
    if not non_shell:
        return "DESKTOP_BLANK"

    for browser_name in ("Google Chrome", "Chromium", "chromium-browser"):
        chrome = root.find(f'.//application[@name="{browser_name}"]')
        if chrome is not None:
            frame = chrome.find(f'.//frame[@{{{_ST}}}active="true"]')
            if frame is None:
                for f in chrome.findall(".//frame"):
                    if f.get(f"{{{_ST}}}showing", "false") == "true":
                        frame = f
                        break
            if frame is None:
                return "BROWSER_MINIMISED"

            url_entry = frame.find('.//entry[@name="Address and search bar"]')
            if url_entry is not None and url_entry.text:
                return f"BROWSER_OPEN:{url_entry.text.strip()}"
            return f"BROWSER_OPEN:{frame.get('name', '')}"

    for term in ("GNOME Terminal", "xterm", "Terminal", "Konsole"):
        if any(term in n for n in app_names):
            return "TERMINAL_OPEN"

    if any(n in ("Files", "Nautilus", "org.gnome.Nautilus") for n in app_names):
        return "FILE_MANAGER_OPEN"

    for editor in ("gedit", "mousepad", "Text Editor", "Kate"):
        for a in apps:
            if editor in a.get("name", ""):
                frame = a.find(".//frame")
                title = frame.get("name", "") if frame is not None else ""
                return f"EDITOR_OPEN:{title}"

    for lo_marker in ("soffice", "LibreOffice"):
        for a in apps:
            if lo_marker in a.get("name", ""):
                frame = a.find(".//frame")
                title = frame.get("name", "") if frame is not None else ""
                return f"LIBREOFFICE_OPEN:{title}"

    return f"UNKNOWN:{','.join(n for n in app_names if n)}"
