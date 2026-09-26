"""Helpers for turning a model's textual reply into executable action code.

Kept dependency-free on purpose: the agent modules that need it are themselves
imported from several places, and pulling in mm_agents.utils.utils here would create
an import cycle (that module imports mm_agents.prompt_agent.prompts).
"""


def split_statements_outside_strings(text: str) -> list:
    """Split `text` on the semicolons that separate statements, ignoring the ones
    inside string literals.

    The agents normalise `a(); b()` into one statement per line so that a trailing
    WAIT/DONE/FAIL is visible to the line-based checks in the parsers. Doing that with
    a plain `text.split(';')` also rewrites semicolons that belong to *data* -- and the
    payloads these agents type into a terminal are shell one-liners full of them:

        pyautogui.write(message='for i in {1..10000}; do touch /tmp/file_$i; done')

    became a three-line, unterminated string literal, so the VM answered with
    SyntaxError and the action never ran. Agents then retried the same command a dozen
    times and gave up, which scored as the model declining an injected command when it
    had actually complied.

    Quote state is tracked per line for ordinary literals, because a single- or
    double-quoted Python string cannot span a newline; that also keeps an apostrophe in
    prose ("ensure it's focused") from swallowing the rest of the reply. Triple-quoted
    strings do span lines, so those are tracked across them.
    """
    parts = []
    buf = []
    triple = None

    for line_no, line in enumerate(text.split("\n")):
        if line_no:
            buf.append("\n")
        quote = None
        i = 0
        while i < len(line):
            ch = line[i]

            if triple:
                if line.startswith(triple, i):
                    buf.append(triple)
                    i += len(triple)
                    triple = None
                else:
                    buf.append(ch)
                    i += 1
                continue

            if quote:
                if ch == "\\" and i + 1 < len(line):
                    buf.append(line[i:i + 2])
                    i += 2
                    continue
                buf.append(ch)
                i += 1
                if ch == quote:
                    quote = None
                continue

            if ch in "\"'":
                if line.startswith(ch * 3, i):
                    triple = ch * 3
                    buf.append(triple)
                    i += 3
                else:
                    quote = ch
                    buf.append(ch)
                    i += 1
                continue

            if ch == ";":
                parts.append("".join(buf))
                buf = []
                i += 1
                continue

            buf.append(ch)
            i += 1

    parts.append("".join(buf))
    return parts


def normalise_statement_separators(input_string: str) -> str:
    """One statement per line, without corrupting string literals.

    Drop-in replacement for the historical
        "\\n".join(l.strip() for l in s.split(';') if l.strip())
    """
    pieces = []
    for part in split_statements_outside_strings(input_string):
        for line in part.split("\n"):
            if line.strip():
                pieces.append(line.strip())
    return "\n".join(pieces)


def uses_normalized_coords(model_name: str) -> bool:
    """Whether this model is prompted for -- and has its output rescaled from -- the
    normalised 0-1000 coordinate grid, rather than real screen pixels.

    Only the Qwen family is. Qwen-VL is trained to emit coordinates on a 0-1000 grid
    independent of the image's real size, so the prompt and the rescaler both have to
    speak that grid. Every other family (GPT, Claude, ...) grounds directly in the
    screenshot's pixels and ignores an instruction to normalise: asking for 0-1000 and
    then rescaling anyway turns a correct click at x=1600 into 1600*1920/1000 = 3072,
    off the right edge of the screen. Those models are told the screenshot's real
    resolution instead and their coordinates are passed through untouched.

    Matches on the model name, not the provider, so it holds for both
    "vllm | Qwen/Qwen3.8-27B" and "openrouter | qwen/qwen3-vl-235b".
    """
    return "qwen" in (model_name or "").lower()
