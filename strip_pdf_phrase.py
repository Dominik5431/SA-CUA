"""Delete "THIS IS IMPORTANT! THIS IS IMPORTANT!" from a PDF.

Used by make_rtc_fixed.sh to clean the OwnCloud injection documents, whose
prompt injection lives in the PDF itself rather than in the task config.

The phrase is removed by rewriting the text-showing operands in the page content
streams.  PyMuPDF redaction is not usable here: it drops every glyph whose
bounding box touches the redaction rectangle, which wipes out neighbouring lines
on the rotated and wide-spaced variants of these documents.  Editing the
operands removes exactly the phrase's glyphs and leaves the rest of the page
alone; the text that followed the phrase on the same line is pulled left so the
deletion does not leave a hole.

    python strip_pdf_phrase.py IN.pdf OUT.pdf   # -> number of matches removed
"""
import re
import sys

import fitz

PHRASE_RE = re.compile(r"THIS\s+IS\s+IMPORTANT!(?:\s*THIS\s+IS\s+IMPORTANT!)*[ \t]*")

DELIM = b"()<>[]{}/%"
WS = b"\x00\t\n\x0c\r "
POSITIONING = {b"Td", b"TD", b"Tm", b"T*", b"BT", b"ET", b"'", b'"', b"Do",
               b"BI", b"EI"}
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\x0c"}


def _unescape(s):
    out = bytearray()
    i = 0
    while i < len(s):
        if s[i:i + 1] == b"\\":
            nxt = s[i + 1:i + 2]
            if nxt in _ESCAPES:
                out += _ESCAPES[nxt]
                i += 2
            elif nxt.isdigit():
                m = re.match(rb"[0-7]{1,3}", s[i + 1:])
                out.append(int(m.group(), 8) & 0xFF)
                i += 1 + len(m.group())
            elif nxt in (b"\n", b"\r"):
                i += 2
            else:
                out += nxt
                i += 2
        else:
            out += s[i:i + 1]
            i += 1
    return bytes(out)


def _tokenize(data):
    """Yield (kind, start, end, payload); kind in {str, name, num, op, other}."""
    i, n = 0, len(data)
    while i < n:
        c = data[i:i + 1]
        if c in WS:
            i += 1
            continue
        if c == b"%":
            while i < n and data[i:i + 1] not in b"\r\n":
                i += 1
            continue
        if c == b"(":
            j, depth, out = i + 1, 1, bytearray()
            while j < n and depth:
                ch = data[j:j + 1]
                if ch == b"\\":
                    out += data[j:j + 2]
                    j += 2
                    continue
                if ch == b"(":
                    depth += 1
                elif ch == b")":
                    depth -= 1
                    if not depth:
                        j += 1
                        break
                out += ch
                j += 1
            yield ("str", i, j, _unescape(bytes(out)))
            i = j
            continue
        if c == b"<" and data[i + 1:i + 2] != b"<":
            j = data.find(b">", i)
            if j < 0:
                j = n - 1
            hx = re.sub(rb"[^0-9A-Fa-f]", b"", data[i + 1:j])
            if len(hx) % 2:
                hx += b"0"
            yield ("str", i, j + 1, bytes.fromhex(hx.decode()))
            i = j + 1
            continue
        if data[i:i + 2] in (b"<<", b">>"):
            i += 2
            continue
        if c in b"<>[]{}":
            yield ("other", i, i + 1, c)
            i += 1
            continue
        if c == b"/":
            j = i + 1
            while j < n and data[j:j + 1] not in WS and data[j:j + 1] not in DELIM:
                j += 1
            yield ("name", i, j, data[i + 1:j])
            i = j
            continue
        j = i
        while j < n and data[j:j + 1] not in WS and data[j:j + 1] not in DELIM:
            j += 1
        if j == i:
            j = i + 1
        tok = data[i:j]
        yield ("num" if re.fullmatch(rb"[-+.0-9]+", tok) else "op", i, j, tok)
        i = j


def _hex_to_str(h):
    if not h:
        return ""
    b = bytes.fromhex(h if len(h) % 2 == 0 else h + "0")
    return b.decode("utf-16-be", "replace")


def _parse_tounicode(stream):
    txt = stream.decode("latin-1", "replace")
    cmap = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", txt, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            cmap[int(src, 16)] = _hex_to_str(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", txt, re.S):
        for lo, hi, dst in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            base = int(dst, 16) if dst else 0
            for k in range(int(lo, 16), int(hi, 16) + 1):
                cmap[k] = chr(base + k - int(lo, 16))
        for lo, hi, arr in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", block, re.S):
            for off, item in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", arr)):
                cmap[int(lo, 16) + off] = _hex_to_str(item)
    return cmap


def _font_widths(doc, page):
    """font resource name -> (first char code, [widths in 1/1000 em])."""
    widths = {}
    for info in page.get_fonts(full=True):
        xref, _ext, subtype, _base, refname = info[:5]
        if subtype == "Type0":
            continue
        kind, first = doc.xref_get_key(xref, "FirstChar")
        if kind != "int":
            continue
        kind, arr = doc.xref_get_key(xref, "Widths")
        if kind == "xref":
            try:
                arr = doc.xref_stream(int(arr.split()[0])).decode("latin-1")
            except Exception:
                continue
        elif kind != "array":
            continue
        nums = [float(x) for x in re.findall(r"-?[0-9.]+", arr)]
        widths[refname] = (int(first), nums)
    return widths


def _font_maps(doc, page):
    """font resource name -> (bytes per code, {code: text})."""
    fallback = {c: chr(c) for c in range(32, 127)}
    maps = {}
    for info in page.get_fonts(full=True):
        xref, _ext, subtype, _base, refname = info[:5]
        nbytes = 2 if subtype == "Type0" else 1
        cmap = {}
        key, val = doc.xref_get_key(xref, "ToUnicode")
        if key == "xref":
            try:
                cmap = _parse_tounicode(doc.xref_stream(int(val.split()[0])))
            except Exception:
                cmap = {}
        maps[refname] = (nbytes, cmap or (fallback if nbytes == 1 else {}))
    return maps


def strip_pdf(src, dst, phrase_re=PHRASE_RE):
    """Write `src` to `dst` with every phrase match deleted.  Returns the count."""
    doc = fitz.open(src)
    total = 0
    for page in doc:
        fonts = _font_maps(doc, page)
        widths = _font_widths(doc, page)
        fallback = (1, {c: chr(c) for c in range(32, 127)})

        streams = []
        for xref in page.get_contents():
            data = doc.xref_stream(xref)
            streams.append([xref, data, list(_tokenize(data))])

        chars, records = [], []
        positions = []
        for si, (_xref, _data, tokens) in enumerate(streams):
            font, size, tc, tw, line_y = None, 0.0, 0.0, 0.0, None
            operands = []
            for ti, (kind, _s, _e, payload) in enumerate(tokens):
                if kind in ("num", "name"):
                    operands.append((kind, ti, payload))
                    continue
                if kind == "str":
                    nbytes, cmap = fonts.get(font, fallback)
                    for off in range(0, len(payload) - nbytes + 1, nbytes):
                        code = int.from_bytes(payload[off:off + nbytes], "big")
                        piece = cmap.get(code, "\ufffd") or "\ufffd"
                        chars.append(piece)
                        records.append((si, ti, off, nbytes, code, font, size,
                                        tc, tw, line_y))
                    operands = []
                    continue
                if kind != "op":
                    operands = []
                    continue
                op, nums = payload, [o for o in operands if o[0] == "num"]
                if op == b"Tf":
                    names = [o for o in operands if o[0] == "name"]
                    if names:
                        font = names[-1][2].decode("latin-1")
                    if nums:
                        size = float(nums[-1][2])
                elif op == b"Tc" and nums:
                    tc = float(nums[-1][2])
                elif op == b"Tw" and nums:
                    tw = float(nums[-1][2])
                elif op in (b"Td", b"TD") and len(nums) >= 2:
                    line_y = float(nums[-1][2])
                    positions.append((si, nums[-2][1], float(nums[-2][2]),
                                      line_y, ti))
                    chars.append("\n")
                    records.append(None)
                elif op == b"Tm" and len(nums) >= 6:
                    line_y = float(nums[-1][2])
                    positions.append((si, nums[-2][1], float(nums[-2][2]),
                                      line_y, ti))
                    chars.append("\n")
                    records.append(None)
                elif op in POSITIONING or op == b"cm":
                    line_y = None
                    chars.append("\n")
                    records.append(None)
                operands = []

        text = "".join(chars)
        matches = list(phrase_re.finditer(text))
        if not matches:
            continue
        total += len(matches)

        starts, pos = [], 0
        for piece in chars:
            starts.append(pos)
            pos += len(piece)
        drop = set()
        mi = 0
        for ri, start in enumerate(starts):
            end = start + len(chars[ri])
            while mi < len(matches) and matches[mi].end() <= start:
                mi += 1
            if mi < len(matches) and matches[mi].start() < end and records[ri]:
                drop.add(ri)

        kept, touched = {}, set()
        slack = {}
        for ri, rec in enumerate(records):
            if rec is None:
                continue
            si, ti, off, nbytes, code, font, size, tc, tw, line_y = rec
            buf = kept.setdefault((si, ti), bytearray())
            if ri in drop:
                touched.add((si, ti))
                if line_y is not None:
                    first, warr = widths.get(font, (0, []))
                    idx = code - first
                    w = warr[idx] if 0 <= idx < len(warr) else 0.0
                    adv = w / 1000.0 * size + tc + (tw if code == 32 else 0.0)
                    entry = slack.setdefault((si, line_y), [0.0, ti])
                    entry[0] += adv
                    entry[1] = max(entry[1], ti)
            else:
                buf += streams[si][2][ti][3][off:off + nbytes]

        edits = {}
        for (si, ti) in touched:
            edits.setdefault(si, {})[ti] = b"<" + bytes(kept[(si, ti)]).hex().encode() + b">"
        for (si, line_y), (delta, last_ti) in slack.items():
            if delta <= 0:
                continue
            for psi, xti, x, y, ti in positions:
                if psi != si or ti <= last_ti:
                    continue
                if abs(y - line_y) > 0.01:
                    break
                edits.setdefault(si, {})[xti] = ("%.4f" % (x - delta)).rstrip("0").rstrip(".").encode()

        for si, (xref, data, tokens) in enumerate(streams):
            if si not in edits:
                continue
            out, prev = bytearray(), 0
            for ti in sorted(edits[si]):
                _k, s0, e0, _p = tokens[ti]
                out += data[prev:s0]
                out += edits[si][ti]
                prev = e0
            out += data[prev:]
            doc.update_stream(xref, bytes(out))

    doc.save(dst, garbage=3, deflate=True)
    doc.close()
    return total


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    print(strip_pdf(argv[1], argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
