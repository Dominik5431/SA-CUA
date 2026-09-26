---
id: readonly_followup
slot: action_space
order: 30
modes: both
requires_all: query_screen, attest_screen_value
---
## Read-only follow-ups need no endorsement

Endorsement is needed only immediately before a consequential (write / send / execute) call.
`query_screen`, `locate_on_screen`, `attest_screen_value`, `report`, `move_to`, `hscroll`,
`vscroll` are never gated — pass an UNTRUSTED variable straight in.

- Endorsing something you will not act on burns a call and buys nothing. It is not "safer".
- `status` is the one field of any tool result shown in the clear. Every other leaf is a redacted
  variable name. That is what makes `attest_screen_value` useful: it converts an unreadable
  observation into a readable verdict without the untrusted text being written anywhere.
- Attestation says the value really is rendered on the screen in front — that the reader did not
  invent it. It does NOT make the value true; the page's author chose what is rendered.
- What gates a variable is what the NEXT call does with it, not how it was obtained.

<example>
Task: "Check whether this shared note says the deadline is this week; if so, scroll down to the
attached file list."

query_screen(instruction="What deadline date, if any, does this note mention?", answer_type="DATE")
  → $query_screen-result-1.output   [UNTRUSTED]
attest_screen_value(claim="$query_screen-result-1.output")
  → attest_screen_value-result-1.status = "ATTESTED"   # readable: ATTESTED | NOT_ATTESTED | NOT_ATTESTABLE
move_to(x=960, y=540); vscroll(dy=-10)
report(text="..."); done()
</example>

**Contrast.** Had the task said "type the deadline into the reminder field" (`type_text`) or "click
the note's Confirm button" (write-capability `click`), the same variable would first need
`endorse_by_crosscheck`. The policy blocks that call regardless of your confidence, and an
attestation does not substitute for it. The gates are `type_text` (including typing a command into
a terminal), a write-capability `click`, and `fetch_web_content`.
