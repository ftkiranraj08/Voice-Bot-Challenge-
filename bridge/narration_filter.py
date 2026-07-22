"""Strips leading 'thinking out loud' narration from the caller-bot's own lines.

Code-level backstop for iteration.md Issue 13: three rounds of prompt-only
fixes in bridge/persona.py reduced how often the model prefaces its real
in-character line with narration about its own response process (e.g. "let
me think about how to respond"), but new phrasings kept leaking through.
This only cleans the transcript log -- see the Issue 14 note in
iteration.md for why the audio itself isn't filtered the same way.
"""

import re

NARRATION_OPENERS = (
    "thanks for",
    "let me",
    "i'm not sure",
    "im not sure",
    "sure,",
    "okay,",
    "ok,",
    "alright,",
)

# Matches the two-clause leak actually observed (e.g. "Hi there, thanks for
# the greeting -- let me think about how to respond. Hi, this is Maria...").
MAX_CLAUSES = 2


def _is_narration(sentence):
    lower = sentence.lower()
    return any(opener in lower for opener in NARRATION_OPENERS)


def strip_narration_prefix(text):
    """Drop up to MAX_CLAUSES leading sentences that match known narration
    openers, stopping as soon as a sentence doesn't match or nothing would
    be left to say. Never returns an empty string if the input was non-empty.
    """
    remaining = text
    for _ in range(MAX_CLAUSES):
        stripped = remaining.lstrip()
        match = re.search(r"[.!?]", stripped)
        if not match:
            break
        sentence, rest = stripped[: match.end()], stripped[match.end() :].lstrip()
        if not rest or not _is_narration(sentence):
            break
        remaining = rest
    return remaining
