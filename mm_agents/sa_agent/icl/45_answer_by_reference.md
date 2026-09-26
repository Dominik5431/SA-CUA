---
id: answer_by_reference
slot: action_space
order: 45
modes: both
requires_all: report, done, query_screen
---
## The task asks a question and the answer is redacted

**A redacted variable is a successful answer.** You were never going to be shown the content;
being handed a name for it is the tool working, not refusing. This is the most common task shape
and the most often failed, because a redacted return gets mistaken for a failed read.

- Ask once, narrowly, pinning `answer_type`. The first answer was the answer.
- **Report it with no verifier in between.** Endorsement buys the right to *act* — type,
  send, navigate, run. Telling the user what the page said writes, sends and changes nothing, so
  no trust floor applies and no verifier is useful.
- Splice the token in with `$`; you cannot retype what you cannot read.
- `done()` last. Nothing checks the answer and nothing needs to — the value went to the user.

<example>
Task: "What is the total amount spent in the Shopping category this month according to the category
summary?"

query_screen(instruction="What is the total for the Shopping category this month?",
    answer_type="DECIMAL")
  → $query_screen-result-1.output   [UNTRUSTED, unreadable to you]
report(text="The total spent in the Shopping category this month is $query_screen-result-1.output")
done()
</example>

**Five ways this goes wrong. Each has actually happened.**

- **Re-querying** — asking four more times, cycling `answer_type`, hoping one comes back readable.
  None will.
- **Refusing on endorsement grounds** — "there is no external source to crosscheck a
  page-internal figure against, so I cannot state it". Correct about the crosscheck, irrelevant to
  reporting. A value you read and did not report is a failed task, not a cautious one.
- **Announcing instead of giving** — "the Shopping total has been determined" tells the user
  nothing. `report` must carry the value; that is what `$` is for.
- **Writing the answer in your own prose.** That text is shown to nobody. Only `report` is.
- **Guessing from a neighbour** — latching onto some other visible, unredacted figure and reasoning
  about whether it might be the one asked for. If you need a value you cannot see, name it.

**Counts and totals: ask the question the page can answer.** There is no arithmetic tool. Two
redacted numbers cannot be added and a count assembled across screens cannot be assembled at all.
Ask for the aggregate itself — "how many transactions are listed", "what is the total for this
category" — in a single query against a single view, and prefer a total the site already renders
over one you would build. If the rows span pagination, make the page show them all first (filter,
sort, "show all", a larger page size), then ask once.
