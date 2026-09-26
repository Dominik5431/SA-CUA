---
id: yes_no_by_status
slot: action_space
order: 46
modes: both
requires_all: report, find_by_text, done
---
## Answering a yes/no question about content you cannot read

Reporting a redacted variable works when the *value* is the answer. Here it is not: the answer is a
judgement about the value, and the word "yes" or "no" has to appear in what you say. `query_screen`
is the wrong tool for this — it would hand back a variable you cannot read and cannot clear, so you
would end the turn knowing less than when you started.

- **Use the tool whose needle YOU authored.** `find_by_text(needle=...)` takes a string you brought
  — here a word from the task itself — and returns a real boolean `present`. Because the string is
  yours rather than the page's, the result is yours too and comes back readable rather than
  redacted. It is deterministic: no model, no network, just "are these glyphs in the accessibility
  tree of the window in front".
- **`attest_screen_value` is the other direction and is not this.** It takes a value a previous
  tool read OFF the page and tells you whether that reader invented it. Reach for it to check an
  extraction, never to ask a question about content.
- **Narrow the screen first, or you answer a different question.** The search covers the whole
  window in front — there is no region argument — so "tomato" matches a tomato soup three rows down
  just as well. Scroll, filter or open the item so that what is in front is the thing being asked
  about, and say in `report` what you actually checked.
- **The needle must be able to settle the question.** Too short or too generic and the call is
  refused (`NEEDLE_REJECTED`) rather than answered, which is correct: "ok" being on screen proves
  nothing. A nonzero `n_look_alikes` means someone put a near-copy on the page; near-copies are
  never counted as matches.
- **`present` is presence, not truth.** It says the glyphs are there, not that the page is honest —
  an attacker can render your word too. That is exactly right for a question ABOUT the page ("does
  this menu list tomato") and wrong for one about the world.
- Because you now know the answer yourself, author it as a **literal** — no template, no variable.

<example>
Task: "Does the Bruschetta on the menu contain tomato?"

[open the Bruschetta entry so its ingredient list is what is on screen]
find_by_text(needle="tomato")
  → present = true            # readable, because the needle was yours
report(text="Yes — the Bruschetta lists tomato among its ingredients.")
done()
# on present = false: "No — tomato is not listed among the Bruschetta ingredients."
</example>

**Say the word.** "Tomato appears in the listing" is not an answer to "does it contain tomato".
Lead with yes or no, then the reason. Same for a question naming a set ("is it a, b, or c?"): name
the one, in the question's own vocabulary.

**Which route for which question.** Asks *what* a value is → report the variable by reference; you
do not need to read it. Asks *whether* something holds, or to choose between named options → bring
the string and get a readable verdict. Never use route two to learn a value by bisection: a
sequence of needles narrowing in on a figure is reading the redacted value the long way round, and
reporting the variable was always available and always cheaper.
