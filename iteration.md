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
Also added an optional `language` field to the scenario config schema 
(config/scenarios.py, defaults to "English" via `scenario.get("language", 
"English")` in persona.py) so a future scenario can deliberately test a 
language switch without new code.
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

---

## Issue 4: Barge-in only flushed Twilio's audio, never cancelled the model's response
**Found in:** Call 01 re-run (post Issue 1/2 fixes), comparing audio playback 
against transcript.txt -- transcript showed only 3 MID-SPEECH INTERRUPT flags 
and full sentences, but the audio had the caller-bot cut off far more often, 
with words in the transcript ("Yes, that's right. It would be a general 
office visit for routine care.", etc.) that were never fully heard, plus call 
duration dropping from ~2:35 to ~1:34 vs. the prior run, and a couple of the 
target agent's own lines (e.g. a trailing "take care" at call end) missing 
from the transcript entirely.
**Root cause:** On `input_audio_buffer.speech_started` (barge-in), the bridge 
sent Twilio a `clear` event (stop playing queued audio) but never sent the 
Realtime API a `response.cancel` -- so the model kept generating and 
transcribing the "interrupted" response as if nothing happened. That produced 
two different truncation points (audio cut off client-side, transcript text 
continuing server-side), left "response in progress" state stuck true for 
far longer than the audio suggested (so our once-per-response-cycle gap 
capture silently missed subsequent real interruptions in the same stuck 
cycle), and likely explains the dropped call duration (composed audio being 
discarded instead of actually played). The missing trailing agent line is a 
separate, smaller bug: on Twilio's "stop" event we cancelled the 
OpenAI-listening task immediately, dropping any transcription still in 
flight for the agent's last utterance.
**Fix:** `bridge/realtime_client.py` -- added `cancel_response()`, sent 
alongside the existing Twilio `clear` on every `input_audio_buffer.
speech_started`, gated on `_response_in_progress` so it's a no-op when 
nothing needs cancelling. `bridge/server.py` -- added a ~2.5s grace window 
(`TRAILING_EVENTS_GRACE_SEC`) after Twilio's "stop" before tearing down the 
OpenAI-listening task, so trailing events already in flight can still be 
captured.
**Verified:** [pending re-run / call XX]