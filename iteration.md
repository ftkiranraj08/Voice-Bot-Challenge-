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
**Verified:** Call 01, 4th run (CA3c543cef5ea014396e3f9c01733fa005) -- trailing 
agent lines and full-sentence caller-bot lines both present, no dropped 
duration (~2:25, in line with prior clean runs).

---

## Issue 5: response.cancel fix exposed a pre-existing false-trigger problem (regression vs. iteration 1/2)
**Found in:** Call 01, 3rd run (CAa53e8d9d4e063b1a0fc64bc96647277a), comparing 
against the 1st and 2nd runs -- audio noticeably worse, caller-bot barely 
got to speak, and new `OpenAI realtime error: response_cancel_not_active 
... no active response found` entries appeared repeatedly in the transcript, 
including right after the agent's very first two lines (00:04.73, 00:07.24) 
-- before the caller-bot had said a single word.
**Root cause:** `response.cancel` (Issue 4's fix) made barge-in genuinely 
end the model's response, whereas before it was a client-side-only, mostly 
cosmetic flush. That surfaced a pre-existing issue: our Realtime session 
opens a `response.created` very early in the call -- before the agent's 
real greeting -- that never produces audio (near-instant/empty). Before 
Issue 4's fix this was harmless (nothing to actually cut off); after it, 
every one of these phantom responses gets flagged as a false MID-SPEECH 
INTERRUPT and triggers a `response.cancel` against a response that's often 
already ended server-side, producing the `response_cancel_not_active` 
errors. Confirmed via temporary debug logging (call 4, CA3c543cef5ea014396e3f9c01733fa005): 
a `response.created`/`response.done` pair 60ms apart, status `cancelled`, 
`output_items=0`, lining up with the agent speaking two utterances back to 
back (no caller-bot turn in between). Our own gated `cancel_response()` 
couldn't have caused it (no audio had been produced yet to satisfy the gate 
added below) -- this is OpenAI's own server-side turn-detection auto-
cancelling a response it hadn't started yet when new input arrives, 
independent of anything we send. Our manual `response.cancel` (Issue 4) was 
occasionally racing that same server-side cancellation, which is what 
produced the `response_cancel_not_active` errors.
**Fix:** `bridge/realtime_client.py` -- gate both interrupt detection and 
`cancel_response()` on the response having actually produced an audio delta 
(`_response_has_audio`), not just on `response.created` having fired. A 
response with no audio yet has nothing audible to interrupt, so it's no 
longer flagged, and we no longer race the server's own auto-cancellation by 
sending a redundant one. `bridge/server.py` -- temporary `[debug]` print 
logging for `response.created`/`response.done` confirmed the above and has 
been removed now that it's served its purpose.
**Verified:** Call 01, 4th run (CA3c543cef5ea014396e3f9c01733fa005) -- 0 
short gaps, 0 mid-speech interrupts, 0 `response_cancel_not_active` errors 
(down from 1 short gap + 3 interrupts + 4 errors in the 3rd run), and the 
two opening agent lines that were falsely flagged as interrupts before 
(00:04, 00:07) now carry no gap annotation at all, as expected for a 
pre-audio phantom response.

---

## Issue 6: caller-bot transcript could silently drop a cancelled turn's partial speech
**Found in:** Design review while answering "should the transcript show only 
what the caller-bot actually spoke, or whatever text the model generated" -- 
not something observed directly in a call yet.
**Root cause:** The transcript only ever flushed the caller-bot's pending 
text on `response.output_audio_transcript.done`. That event normally fires 
when a response finishes normally, but it's not guaranteed to fire the same 
way for a response that got cut off mid-generation by `response.cancel` 
(Issue 4/5). If it doesn't fire, whatever partial sentence the bot had 
already composed -- and very likely already started speaking, since audio 
and its transcript are generated together -- would just vanish from the 
transcript with no trace, understating what was actually said rather than 
overstating it (the opposite failure mode from Issue 4).
**Fix:** `bridge/server.py` -- also flush the caller-bot's pending text on 
`response.done` (which reliably fires for every response, cancelled or not), 
as a safety net on top of the existing `response.output_audio_transcript.
done` flush. `flush()` is a no-op if there's nothing pending, so this only 
matters for the cancelled-mid-sentence case.
**Design intent (answering the question directly):** the transcript should 
reflect what the caller-bot actually said, not everything the model 
internally generated. In practice these are almost the same thing, since 
Realtime API composes audio and its transcript together turn by turn -- the 
only place they can diverge is a cancelled/interrupted turn, where a few 
hundred ms of already-buffered-but-unplayed audio might get flushed by 
Twilio's `clear` after the model already committed to generating text for 
it. That's a small, structural gap in a live-audio system, not something 
worth engineering around further right now.
**Verified:** [still pending -- call 4 (CA3c543cef5ea014396e3f9c01733fa005) had 
zero genuine mid-speech interrupts, so this safety net wasn't exercised. 
Needs a run with a real interrupt (e.g. scenario 10_interruption_barge_in) 
to confirm the cancelled turn's partial line still shows up.]

---

## Issue 7: added an audio-byte-based cross-check on gap_ms (not a bug fix -- new instrumentation)
**Found in:** Discussion while reviewing call 4 -- user asked whether an
unflagged agent line (01:48.26, the zero-output phantom response from Issue
5) or the repeated date-of-birth exchange (00:30-00:43) represented a real
interruption our event-based gap_ms would miss. Root question: gap_ms trusts
response.done's timestamp for "when did our bot finish talking," but
Twilio's `clear` (Issue 4) fires unconditionally on every speech_started
regardless of response state -- so in theory a response the model considers
fully "done" could still have its last word clipped audibly if the agent
starts talking again before Twilio finishes playing out its buffer. gap_ms
alone can't detect that gap between "model says done" and "phone line
actually finished playing."
**Fix (instrumentation, not a bug fix):** `bridge/realtime_client.py` --
track the exact byte count of every `response.output_audio.delta` per
response. G.711 (audio/pcmu) is a fixed-rate codec (8000 bytes/sec exactly),
so `bytes / 8000` gives the *exact* audio duration, no word-count estimation
needed. `last_gap_ms_audio` computes the gap the same way as `last_gap_ms`
but measured from when the audio should actually finish playing (first-byte
time + computed duration) instead of `response.done`'s timestamp.
`bridge/server.py`/`bridge/transcript.py` -- both numbers now render
side by side per turn (`gap: Xms, audio-check: Yms`), and the summary calls
out any turn where they disagree by >300ms as "possible clipped audio."
**Verified (synthetic offline test only):** confirmed the two methods
diverge sharply in a simulated clipping scenario (+100ms event-based vs.
-845ms audio-based) and agree closely in a normal one (1004ms vs. 1009ms).
Not yet verified against a real call -- next run's transcript will show
whether any real turns actually disagree by more than the threshold, which
would be the first hard evidence of `clear` clipping legitimately-finished
audio.