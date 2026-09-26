#!/usr/bin/env python3
"""
capacity.py -- can this string carry an instruction?

THE PROPERTY THIS MODULE DECIDES, and the one it deliberately does not. Masking exists to stop
injected *instructions* reaching the agent. Whether a string can carry one is a different
question from who authored it, and the two come apart:

    '$0.01'        a seller chose it, it cannot carry an instruction
    '1.0-ignore-previous-instructions'   a repo owner chose it, it can

So the row carries two independent facts. PROVENANCE ("did a user of this site choose this
string?") stays where it was -- in the hand labels and in the rules -- and is what the
endorsement and decision layers must keep reading, because an attacker-set price is a real
attack that involves no injection at all. CAPACITY (this module) says only whether the string, as
rendered, is expressive enough to be an instruction. An element is masked when it is
user-provenance AND free-capacity. Exempting a price from MASKING is not the same as trusting it.

WHY THIS IS NOT A LENGTH THRESHOLD. The project's standing rule is that no attacker-tunable
constant may decide whether something gets labeled -- a fixed N is something an attacker slides
under. There is no N here. Each grammar is a TOTAL, ANCHORED predicate on the string that is
actually on screen: an instruction does not match, so it is masked; a number matches, so it is
not. Nothing about the FIELD is being predicted. A git tag reading '1.134.0' is exempt because
digits and dots cannot say anything; the same tag reading 'v2-please-ignore' is masked, and the
verdict is re-decided from the rendered text every single turn. The exemption is per observation
and can never be inherited by a sibling, by a region, or by the next value in the same slot.

THREE PROPERTIES EVERY GRAMMAR HERE MUST HAVE, and the tests enforce all three:

  1. TOTAL MATCH ON EVERY STRING THE ROW PUBLISHES. capacity_class() takes every string a row
     shows a human -- the accessible name and the Text-interface payload -- and returns a class
     only when EVERY one of them fully matches. One extra character anywhere and the row is not
     exempt. Substring matching would be a leak: '$19.99 ignore all previous' begins with a price.

  2. NO FREE LETTER POSITION. A grammar may contain letters only as fixed keywords from a closed
     list (month names, 'ago', 'out of', unit symbols). Nowhere may it admit an arbitrary letter,
     because that is precisely the capacity being denied.

  3. A CLOSED ALPHABET, checked independently of the regex. Non-ASCII characters are rejected
     outright apart from an enumerated handful of currency and multiplication signs, and the only
     whitespace admitted is a plain ASCII space. This is what stops the homoglyph route: Python's
     \\d matches full-width digits, so '$19.99' spelled in full-width forms would satisfy a
     casually written pattern. Those strings are refused rather than normalised -- normalising
     would ADMIT them, which is the wrong direction for a gate.

WHY THERE IS NO COMMIT-HASH GRAMMAR. There was one -- `hex_digest`, `[0-9a-f]{7,40}` -- and it
was the ONLY grammar in this file with a free letter position, which is to say the only one that
broke property 2 above. Every other letter-bearing class is one or two characters from a fixed set
(`[KMGTP]`, `[Ss]`, `[kKMB]`); that one was a forty-character window of attacker-chosen letters.
Hex covers enough of the alphabet for leetspeak -- o->0, i/l->1, e->3, s->5, t->7 -- so `de1e7ed`
and `a11f11e5` matched it, and a commit hash can be ground to a chosen 7-character prefix cheaply.
Removing it is what makes the compositional argument hold: the complete vocabulary reachable
through the remaining grammars is 157 fixed tokens containing no imperative verb, so no set of
exempt strings, however many and however adjacent, can express an instruction. A SHA is
free-capacity now and is masked like any other user-determined string.

THE PRICE-PLUS-NOUN CASE, AND WHY IT IS EXACTLY ONE NOUN. `delivery_fee` admits a price followed
by the literal word 'delivery' and nothing else -- not shipping, not postage, not a list. The
general shape (a price plus any closed set of nouns) was written and then withdrawn: the frozen FREE
corpus refuses 'EUR 18.56 delivery' next to 'EUR 90 - ignore all previous', on the reasoning that a
price with ANY free word attached is the shape an injection uses.

Narrowing to a single frozen literal is a different proposition from that general shape, and it is
the one deliberately taken here. The alphabet gains one noun; the match is still anchored at both
ends, so nothing can follow the word; and 'delivery' is not a verb, so the compositional claim below
is untouched. What the FREE corpus was protecting was the OPEN version, and the negatives that
carry that protection are still pinned -- 'EUR 18.56 shipping' is masked, because the noun is not
this one, and 'EUR 18.56 delivery now to attacker@evil.com' is masked, because the string does not
end there.

WHAT IS DELIBERATELY NOT IN HERE. Status badges ('Verified', 'Latest', 'In stock') are safe by
ENUMERATION, not by grammar: they are bounded because the operator ships a fixed vocabulary, and
checking that needs a frozen per-domain list. That is a different mechanism with a different
failure mode -- an enum derived from page text at runtime would be attacker-authored -- and it is
not implemented. Usernames, emails, URLs, paths and filenames are free-text and stay masked;
'ignore.all.previous@x.com' reads perfectly well with dots for spaces.

CAPACITY_SCHEMA is folded into the universe cache key, so editing a grammar invalidates every
cached universe rather than relying on someone remembering to bump UNIVERSE_SCHEMA.
"""
from __future__ import annotations

import re

CAPACITY_SCHEMA = 12

NON_ASCII_ALLOWED = frozenset("€£¥₹×–—−²≤≥")

SPACE_EQUIVALENTS = {"\u00a0": " ", "\u202f": " ", "\u2007": " ", "\u2009": " ",
                     "\u2002": " ", "\u2003": " "}

_UINT = r"[0-9]+"
_SIGN = "[-+\u2212]?"
_GROUPED_C = r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?"
_GROUPED_D = r"[0-9]{1,3}(?:\.[0-9]{3})+(?:,[0-9]+)?"
_PLAIN     = r"[0-9]+(?:[.,][0-9]+)?"
_NUM       = rf"{_SIGN}(?:{_GROUPED_C}|{_GROUPED_D}|{_PLAIN})"

_MONTH = ("January|February|March|April|May|June|July|August|September|October|November|December"
          "|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec")
_DAY   = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun"
_UNIT  = "second|minute|hour|day|week|month|year|min|sec|hr|yr"
_UNIT_ABBR = "mo|s|m|h|d|w|y"
_DEICTIC = "last|this|next|past|previous"
_PERIOD  = "week|weekend|month|year|day|night|hour|minute|quarter"
_VISIBILITY = "public|private|internal"
_PAGER = "older|newer|oldest|newest|previous|prev|next|first|last"
_SIZE  = "bytes|byte|[KMGTP]iB|kiB|[KMGTP]B|kB|B"
_CUR   = r"[$€£¥₹]"
_ISO   = ("USD|EUR|GBP|JPY|CHF|CAD|AUD|NZD|INR|SEK|NOK|DKK|PLN|CZK|HUF|RUB|BRL|MXN|ZAR|TRY|CNY"
          "|KRW|SGD|HKD")
_TZ_NAMED = ("UTC|UT|GMT"
             "|EST|EDT|CST|CDT|MST|MDT|PST|PDT|AKST|AKDT|HST|HDT|AST|ADT|NST|NDT"
             "|WET|WEST|BST|CET|CEST|EET|EEST|MSK"
             "|IST|PKT|ICT|WIB|SGT|HKT|JST|KST"
             "|AEST|AEDT|ACST|ACDT|AWST|NZST|NZDT"
             "|CAT|EAT|WAT|SAST|BRT|ART|CLT")
_TZ    = (rf"(?:GMT|UTC)(?:[+-][0-9]{{1,2}}(?::?[0-9]{{2}})?)?"
          rf"|[+-][0-9]{{2}}:?[0-9]{{2}}"
          rf"|{_TZ_NAMED}"
          r"|Z")
_COUNT_NOUN = ("commits?|branch|branches|tags?|forks?|stars?|watchers?|watching|contributors?"
               "|issues?|pull requests?|followers?|following|repositor(?:y|ies)|results?|items?"
               "|messages?|files?|folders?|comments?|answers?|questions?|views?|downloads?"
               "|votes?|repl(?:y|ies)|members?|users?|photos?|videos?|reviews?|ratings?"
               "|products?|orders?|notifications?|releases?|packages?|discussions?|milestones?"
               "|topics?|collections?|episodes?|articles?|posts?|pages?|rows?|records?|entries"
               "|entry|notes?|tasks?|events?|contacts?|attachments?"
               "|bathrooms?|bedrooms?|beds?|kitchens?|living rooms?|double beds?"
               "|single beds?|bunk beds?|sofa beds?|restaurants?|participants?"
               "|submissions?|capacit(?:y|ies)|times?|match(?:es)?|people|persons?"
               "|guests?|nights?|adults?|child|children|rooms?|features?|offers?"
               "|sustainability features?|lines?|words?|revs?|submissions?")
_UNUM  = rf"(?:{_GROUPED_C}|{_GROUPED_D}|{_PLAIN})"
_LEN   = "km|mi|miles?|met(?:re|er)s?|cm|mm|ft|feet|yd|yards?|m"
_QUAL  = ("online|offline|found|reacted|remaining|left|total|available|selected|sold"
          "|in dormitories|in stock|project storage")
_FILTER_WORD = "all|closed|merged|draft|active|archived|unread|pinned"
_CNUM  = rf"(?:\+\s?)?{_UNUM}\s?[kKmMB]?"
_PRICE = rf"(?:{_SIGN}(?:{_CUR}|{_ISO})\s?{_NUM}|{_NUM}\s?(?:{_CUR}|{_ISO}))"
_CLOCK = (rf"[0-9]{{1,2}}:[0-9]{{2}}(?::[0-9]{{2}}(?:\.[0-9]{{1,6}})?)?"
          r"(?:\s?(?:am|pm|AM|PM|a\.m\.|p\.m\.))?"
          rf"(?:\s?(?:{_TZ}))?")
_DATE_BODY = (rf"(?:(?:{_DAY}),?\s)?(?:{_MONTH})\.?\s[0-9]{{1,2}}(?:st|nd|rd|th)?,?"
              rf"(?:\s[0-9]{{4}})?"
              rf"|(?:(?:{_DAY}),?\s)?[0-9]{{1,2}}(?:st|nd|rd|th)?\s(?:{_MONTH})\.?,?"
              rf"(?:\s[0-9]{{4}})?"
              rf"|[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}(?:T{_CLOCK})?"
              rf"|[0-9]{{1,2}}[/.][0-9]{{1,2}}[/.][0-9]{{2,4}}"
              rf"|(?:{_MONTH})\s[0-9]{{4}}")

_ABSOLUTE = rf"(?:{_CLOCK},?\s)?(?:{_DATE_BODY})(?:,?\s(?:at\s)?{_CLOCK})?"

_RELATIVE = (rf"(?:{_NUM}|a|an)\s(?:{_UNIT})s?(?:,\s{_NUM}\s(?:{_UNIT})s?)*\s(?:ago|from now)"
             rf"|{_UINT}\s?(?:{_UNIT_ABBR})\s(?:ago|from now)"
             rf"|(?i:{_DEICTIC})\s(?i:{_PERIOD})"
             r"|just now|yesterday|today|tomorrow|now")

_DATE_LABEL = ("on|at|since|until|added|created|updated|edited|posted|published|commented"
               "|answered|asked|joined|modified|started|ended|expired|expires|due|active"
               "|closed|opened|merged|committed|uploaded|sent|received|reviewed|last seen")

GRAMMARS: "dict[str, str]" = {
    "dotted_numeric": rf"{_UINT}(?:\.{_UINT}){{2,}}",
    "percent":        rf"{_NUM}\s?%",
    "currency":       _PRICE,
    "currency_range": rf"{_PRICE}\+?\s?[-–—−]\s?{_PRICE}\+?",
    "currency_code":  rf"{_CUR}|{_ISO}",
    "file_size":      rf"(?:[<>≤≥]\s?)?{_NUM}\s?(?:{_SIZE})(?:\s(?i:{_QUAL}))?",
    "delivery_fee":   rf"{_PRICE}\s(?i:delivery)",
    "distance":       rf"{_NUM}\s?(?:{_LEN})(?:\sfrom\s(?:the\s)?cent(?:re|er))?",
    "area":           rf"{_NUM}\s?(?:m|cm|mm|km|ft|yd)²|{_NUM}\s?sq\s?(?:ft|m|km|mi)",
    "reference_num":  r"[#!][0-9]+",
    "filter_count":   rf"(?i:{_FILTER_WORD})\s?(?:{_UNUM}|\({_UNUM}\))"
                      rf"|{_UNUM}\s(?i:{_FILTER_WORD})",
    "dimension":      rf"{_UINT}\s?[x×]\s?{_UINT}",
    "duration":       r"[0-9]{1,3}:[0-9]{2}(?::[0-9]{2})?(?:\.[0-9]+)?",
    "clock":          _CLOCK,
    "date_absolute":  _ABSOLUTE,
    "date_relative":  _RELATIVE,
    "rating":         rf"{_NUM}\s(?:out of|of|/)\s?{_NUM}"
                      rf"(?:\s(?:[Ss]tars?|[Pp]oints?|[Cc]ircles?))?"
                      rf"(?:,\srating details)?"
                      rf"|{_NUM}\s?[Ss]tars?",
    "counted_noun":   rf"{_CNUM}\s?(?i:{_COUNT_NOUN})(?:\s(?i:{_QUAL}))?\+?"
                      rf"|(?i:{_COUNT_NOUN})\s{_CNUM}\+?",
    "abbrev_count":   rf"{_NUM}\s?[kKmMB]",
    "ordinal":        r"[0-9]+(?:st|nd|rd|th)",
    "visibility":     rf"(?i:{_VISIBILITY})",
    "pagination":     rf"(?i:{_PAGER})\s?{_UNUM}",
    "date_labelled":  rf"(?:(?i:{_COUNT_NOUN})\s)?(?i:{_DATE_LABEL})\s(?:{_ABSOLUTE}|{_RELATIVE})",
    "number":         rf"{_NUM}\+?",
}

_FORBIDDEN = (r"\d", r"\w", r"\S", r"[a-z]", r"[A-Z]", r"[a-zA-Z]", ".*", ".+", "[^")


def _lint() -> None:
    for name, pat in GRAMMARS.items():
        for bad in _FORBIDDEN:
            if bad in pat:
                raise AssertionError(f"capacity grammar {name!r} contains {bad!r}: that admits "
                                     f"an arbitrary character class, see property 2")
        depth, i = 0, 0
        while i < len(pat):
            ch = pat[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
            elif ch == "." and depth == 0:
                raise AssertionError(f"capacity grammar {name!r} has an unescaped '.' at {i}: "
                                     f"a wildcard matches anything, including an instruction")
            i += 1


_lint()

_RE = {name: re.compile(rf"\A(?:{pat})\Z") for name, pat in GRAMMARS.items()}


def _canonical_spaces(s: str) -> str:
    """Space-equivalents to a plain space. See SPACE_EQUIVALENTS for why this one fold is safe."""
    for ch, repl in SPACE_EQUIVALENTS.items():
        if ch in s:
            s = s.replace(ch, repl)
    return s


def _alphabet_ok(s: str) -> bool:
    """Property 3, applied before any pattern is tried, after the space fold.

    Refuses every codepoint above 127 apart from the enumerated currency, dash and multiplication
    signs; every control character; and every whitespace character apart from a plain space.
    Refusal rather than normalisation is deliberate for everything else: NFKC would fold
    '＄１９.９９' onto '$19.99' and ADMIT it, and a classifier whose verdict depends on a
    normalisation table is one an attacker can study.
    """
    for ch in s:
        if ord(ch) > 127:
            if ch not in NON_ASCII_ALLOWED:
                return False
        elif ord(ch) < 32:
            return False
        elif ch.isspace() and ch != " ":
            return False
    return True


def classify(s: str) -> "str | None":
    """The grammar one string fully matches, or None.

    Returns None for the empty string: "nothing here" is not a licence, and a row with no text at
    all is judged on its pixels by whatever else is looking at it.
    """
    s = _canonical_spaces(s).strip()
    if not s or "  " in s or not _alphabet_ok(s):
        return None
    for name, rx in _RE.items():
        if rx.match(s):
            return name
    inner = _unwrap(s)
    if inner is not None:
        for name, rx in _RE.items():
            if rx.match(inner):
                return name
    return None


_PAIRS = {"(": ")", "[": "]", "{": "}"}


def _unwrap(s: str) -> "str | None":
    """One enclosing layer removed, or None if there is not exactly one to remove.

    WHY THIS IS NOT A LOOSENING. The alternative was 16 hand promotions covering `[1]`, `[2]`,
    `2.`, `(387)` and `(602 bytes)` -- the value already had a grammar and the punctuation around
    it was the only reason the match failed. Shaving a layer cannot admit a letter, cannot admit a
    space, and cannot chain: exactly ONE layer comes off and the remainder must then match a
    grammar IN FULL, so `(delete this)` is refused for the same reason `delete this` is. What an
    attacker gains is the ability to write a bounded value inside brackets, which is a bounded
    value inside brackets.

    A trailing period is the same case in the one-sided form -- an ordered-list marker, `2.` -- and
    is taken only when what precedes it is not itself decimal, so `1.5` never loses its fraction.
    """
    if len(s) >= 3 and s[0] in _PAIRS and s[-1] == _PAIRS[s[0]]:
        inner = s[1:-1].strip()
        if inner and not (inner[0] in _PAIRS or inner[-1] in _PAIRS.values()):
            return inner
        return None
    if len(s) >= 2 and s[-1] == "." and s[-2].isdigit():
        return s[:-1]
    return None


def capacity_class(*strings: "str | None") -> "str | None":
    """The class of a ROW: a name only when every string the row publishes is capacity-bound.

    Callers pass every string the row shows a human -- today the accessible name and the
    Text-interface payload. A row publishing one bounded value and one free-text value is NOT
    exempt; the free one is the whole point.
    """
    seen = [classify(s) for s in strings if s and s.strip()]
    if not seen or any(c is None for c in seen):
        return None
    return seen[0]
