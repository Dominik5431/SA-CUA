#!/usr/bin/env python3
"""Remove the pre-posted user question from a rocketchat suite's decouple steps.

Companion to strip_pdf_phrase.py, called by make_rtc_fixed.sh when it builds
examples/rocketchat_fixed/.

In the original suite the decouple step types the opening question into the DM on the
agent's behalf ("Hi Mike Chen, how to download nodejs?") and then sleeps, so the agent
inherits a conversation it never had. Worse, the NPC needs longer to answer than that
sleep allows, so what the agent usually inherited was its own question with no reply --
and a task where the injected answer never arrived scored the same as one where the
agent saw the payload and refused it.

Dropping the "message" key leaves the decouple doing only what it should: open the DM.
The asking is then the agent's job, which is what the instruction asks of it, and the
answer arrives while the agent is watching for it.

Usage:  strip_rocketchat_seed.py <examples/rocketchat_fixed dir>
Prints the number of configs changed.
"""
import glob
import json
import os
import sys


def strip(directory):
    changed = 0
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        with open(path) as fh:
            config = json.load(fh)
        hit = False
        for step in config.get("config", []):
            if step.get("type") == "decouple" and "message" in step.get("parameters", {}):
                del step["parameters"]["message"]
                hit = True
        if hit:
            with open(path, "w") as fh:
                json.dump(config, fh, indent=4)
                fh.write("\n")
            changed += 1
    return changed


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(strip(sys.argv[1]))
