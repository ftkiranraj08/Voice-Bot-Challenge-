# Iteration Log

Chronological record of issues found in the caller-bot itself (not the 
target agent) during testing, and the fixes applied. This is separate from 
BUG_REPORT.md, which documents issues found in Pivot Point's agent.

---

## Issue 1: Persona breaking character (name construction)
**Found in:** Call 01 (01_new_appointment), 00:17
**Symptom:** Caller-bot said "Alright, let me think of a name to get started" 
before stating its assigned name, narrating its own persona construction 
instead of speaking naturally in character.
**Root cause:** System prompt presented caller identity as available info 
rather than an already-known fact, leaving room for the model to "reason" 
about it out loud.
**Fix:** Added explicit instruction in bridge/persona.py: caller is framed 
as "not an AI assistant... a real person," with identity treated as fixed 
knowledge, not something to construct.
**Verified:** [pending re-run / call XX]

---

## Issue 2: Caller-bot didn't hold its selected language
**Found in:** Call 01, 00:07-00:37
**Symptom:** Caller selected Spanish via IVR ("Presiono el 2 para Español"), 
spoke one Spanish utterance, then drifted to English for the rest of the 
call with no in-character reason given.
**Root cause:** Likely mirrored the target agent's English reply rather 
than holding its own assigned language. Also intersects with a real target-
agent bug (see BUG_REPORT.md #X) -- the agent didn't honor the Spanish 
selection either, which may have contributed to the caller-bot's drift.
**Fix:** Added language-consistency instruction: caller holds its selected 
language for the full call regardless of what language the agent responds in.
**Verified:** [pending re-run / call XX]

---

## Issue 3: Turn-taking gap not measured (tooling gap, not a bot bug)
**Found in:** Reviewing call 01 manually for barge-in issues
**Symptom:** Suspected the target agent was interrupting the caller-bot 
with near-zero gap, but had no quantitative way to confirm vs. eyeballing 
audio/transcript.
**Fix:** Added gap_ms instrumentation in realtime_client.py/server.py/
transcript.py -- measures time from caller-bot's audio finishing to the 
target agent's speech resuming, flags SHORT GAP (<150ms) and MID-SPEECH 
INTERRUPT (negative gap) cases automatically in each transcript's summary.
**Impact:** Turned a one-off audio-verified observation into a quantifiable, 
reproducible metric across all calls. 