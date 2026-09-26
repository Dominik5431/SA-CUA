"""Constants shared by every agent implementation in this package.

Kept import-free on purpose: `mm_agents` is imported by `token_usage`, by the agent
loops and by the standalone runners, and a heavyweight import here would be paid by
all of them.
"""

MAX_IMAGE_HISTORY_LENGTH = 10
