# Iteration Log
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

## Issue 4: Recording download 404s right after call completion (tooling gap, not a bot bug)
**Found in:** Call 01 re-run -- `run_calls.py` crashed with 
`requests.exceptions.HTTPError: 404` fetching the recording .mp3, killing 
the script before metadata/recording_path was ever written. Transcript was 
unaffected (written independently by the bridge server).
**Root cause:** `wait_for_recording()` returned as soon as a recording 
resource appeared in Twilio's list, but the underlying media isn't 
guaranteed fetchable until the resource's `status` flips to "completed" -- 
there's a short post-call processing window where the recording exists but 
its .mp3 404s. Confirmed by re-querying the same recording SID afterward: 
status was "completed" and it downloaded fine.
**Fix:** `wait_for_recording()` (twilio_client.py) now polls until 
`status == "completed"` (timeout raised 60s -> 90s) instead of just 
existence. `download_recording()` retries up to 4x with a 5s backoff on 
HTTP errors. `run_calls.py` also now catches exceptions around the 
recording step per-call instead of letting one flaky download abort the 
rest of the batch -- failure is recorded as `recording_error` in that 
call's metadata and the run continues.
**Recovered:** Manually re-downloaded call 01's recording from its existing 
(by-then-completed) Twilio recording SID rather than re-placing the call.
**Verified:** recordings/01_new_appointment.mp3 downloaded successfully 
(603KB, 151s, matches call duration).

---

## Issue 5: Gap metric missed interrupts on longer bot utterances (tooling gap, not a bot bug)
**Found in:** Manually spot-checking call 01, ~00:51 -- agent audibly cut 
the caller-bot off mid-sentence right after "my date of birth," but the 
transcript logged it as a normal 2717ms gap, not a MID-SPEECH INTERRUPT.
**Root cause:** Gap tracking used `response.done` (fires when the model 
finishes *generating* a response) as the "bot stopped talking" timestamp. 
But generation can finish well before Twilio finishes *playing* that audio 
out in real time, especially for longer utterances. So for any interrupt 
landing after generation completed but before playback actually finished, 
the tracker had already reset its "in progress" flag and mis-measured it as 
a large, falsely-positive normal gap instead of a negative interrupt.
**Fix:** Replaced the response.done-based timestamp with a playback-end 
estimate computed from the audio itself: G.711 mu-law @ 8kHz is a fixed 
8000 bytes/sec, so summing each `response.output_audio.delta` chunk's byte 
length (bridge/realtime_client.py) gives an accurate running estimate of 
when the audio will actually finish playing. Gap is now always 
`speech_started_time - estimated_playback_end_time` in one unified 
formula -- negative means the agent spoke before playback truly ended 
(interrupt), positive means it waited (normal/short gap). No more separate 
"in progress" branch needed.
**Verified:** Unit-simulated the exact failure case (1.0s of audio 
generated instantly, agent "interrupts" 0.3s in) -- now correctly yields a 
negative gap (~-695ms) flagged as MID-SPEECH INTERRUPT, vs. a control case 
waiting past the true audio end (~+301ms, correctly not flagged). Full 
confirmation needs a call re-run to see it against a real target-agent 
interrupt.

---

## Issue 6: Interrupted responses kept generating (both a bot bug and a tooling gap)
**Found in:** Call 01 re-run (CA75eb7ae68fe87a9157fa6e8f1c3a8cf8), multiple 
turns e.g. 01:05, 01:31, 01:40 -- caller-bot's line cut off mid-sentence 
with the intended continuation ("If there's any preference for a particular 
provider...", etc.) missing from the transcript entirely, AND the 
following AGENT line not flagged MID-SPEECH INTERRUPT despite an audible 
interruption.
**Root cause:** On barge-in, the bridge only sent Twilio a `clear` event to 
stop already-queued audio from playing -- it never told OpenAI to stop 
*generating* the response. The model kept producing the rest of its 
planned sentence regardless: more audio deltas (bleeding through as audio 
the agent shouldn't hear), more transcript deltas that had nowhere clean 
to flush, and a continuously-growing playback-end estimate (from Issue 5's 
fix) that no longer reflected the true interrupt point by the time the 
next comparison happened -- explaining both symptoms as one cause.
**Fix:** Added `RealtimeClient.cancel_response()` (sends `response.cancel`) 
and call it in bridge/server.py's speech_started handler right alongside 
the existing Twilio `clear`. Also explicitly flush the pending caller-bot 
transcript buffer at that same moment instead of waiting on a 
`response.output_audio_transcript.done` that a cancelled response may 
never cleanly send -- so whatever the bot actually got out before being 
cut off is captured, nothing more.
**Verified:** Import/smoke-tested only so far. Needs a call re-run to 
confirm against a real interrupt -- flagging as the next verification step.

---

## Issue 7: Persona still narrating itself when the agent said something confusing
**Found in:** Calls 02, 03, 04 -- e.g. call 02 00:38 ("I'm not sure if it's 
Maria. I've just called in, and I'm not able to confirm who I'm speaking 
with..."), call 03 00:40 ("Let me confirm that and then we can go from 
there."), call 04 00:25 ("Okay, let me respond to that and keep things 
moving toward the refill request.No, this is James Whitfield...").
**Symptom:** Issue 1's fix stopped the bot from narrating persona 
*construction* at call-open, but a related failure mode survived: whenever 
the agent said something confusing or wrong (most often misidentifying the 
caller -- see the phone-number-identity bug in BUG_REPORT.md), the bot 
would preface its actual answer with meta-commentary about how it was 
about to respond, and in one case (call 02) hedged on its own identity 
("I'm not sure if it's Maria") instead of firmly correcting it.
**Root cause:** The original fix only told the model not to narrate 
constructing its persona at the start of the call. It didn't cover the 
broader pattern of narrating its own response process on any turn, or 
give explicit instruction on how to react when the agent gets the 
caller's identity wrong -- so the model fell back to assistant-style 
"let me help you with that" framing under exactly the conditions (agent 
confusion) where a real person would just answer.
**Fix:** Extended bridge/persona.py: the "don't narrate" instruction now 
explicitly applies to every turn, not just the first, with the specific 
phrasings observed as counter-examples. Added a dedicated instruction for 
misidentification specifically -- react immediately and firmly with the 
actual correction (e.g. "No, this is James Whitfield"), never a hedge or 
a warm-up sentence. Reinforced with a matching bullet in Ground rules, 
mirroring how Issue 1's fix was structured (stated once in the main body, 
once in ground rules).
**Verified:** Rendered prompt reviewed for correct {caller_name} 
substitution in both places; import/smoke-tested only. Needs a call 
re-run against a misidentification moment to confirm.

---

## Issue 8: Every call was fighting a misidentification detour before reaching its actual test goal
**Found in:** Reviewing calls 02-09 -- every single one gets asked "Am I 
speaking with Maria?" partway through, regardless of that scenario's own 
caller_name (David Okafor, Priya Natarajan, James Whitfield, etc.), then 
burns a large chunk of call time on name/DOB re-verification before ever 
reaching the scenario's actual goal (see the 02_reschedule_appointment 
findings in BUG_REPORT.md, where identity handling consumed the entire 
call and the reschedule request was never processed at all).
**Root cause:** All calls place from the same fixed TWILIO_PHONE_NUMBER, 
and the target agent persistently associates that number with "Maria" 
(set on the very first call, 01_new_appointment). Every subsequent call 
using a different caller_name collides with that cached identity, and the 
resulting confusion/re-verification loop eats call time that should be 
spent exercising each scenario's actual probe.
**Fix:** Set caller_name to "Maria Chen" for every scenario in 
config/scenarios.py (previously each had a distinct name). This isn't a 
persona-quality fix -- it's a test-environment adaptation to a constraint 
of reusing one Twilio number across all calls, done so each call's time 
is spent testing what it's meant to test rather than re-litigating 
identity every time. Note this also means the misidentification/
re-verification bugs already logged in BUG_REPORT.md (call 02) won't 
reproduce as reliably going forward, since the guessed name will now 
usually be correct -- that finding is already captured and doesn't need 
to keep re-triggering to stay valid.
**Verified:** Import/smoke-tested; confirmed all 23 scenarios now render 
"Maria Chen" in the persona prompt. Needs a call re-run to confirm the 
agent skips the misidentification detour in practice.

---

## Issue 9: Made-up DOB/phone meant the bot could never actually pass verification, and it denied its own name when asked "is this Maria?"
**Found in:** Reviewing calls 05-23 (post-Issue-8) -- the headline finding in 
BUG_REPORT.md (identity verification never succeeds in any call). Two 
compounding causes on our side, on top of the target's own broken 
verification: (1) the persona was told to "make up plausible specifics" for 
DOB, so it stated a different DOB on every single call -- even if the 
target's verification worked, a different DOB every time would always 
mismatch whatever's on file; (2) at least one transcript showed the bot 
responding "No, I'm Maria Chen" to "is this Maria?" -- denying a question 
it should have confirmed, since Maria is its own name.
**Fix:** Added CALLER_DOB ("July 4th, 2000" -- read back verbatim by the 
agent in an early call's demo-profile setup) and CALLER_PHONE ("(213) 
238-6567" -- the actual TWILIO_PHONE_NUMBER, i.e. what caller-ID shows 
regardless of what's said) as fixed constants in config/scenarios.py, 
injected into every scenario as date_of_birth/phone_number fields 
(overridable per-scenario later if a test wants a deliberate mismatch). 
bridge/persona.py now states these as fixed facts rather than "make up 
something," and the identity-correction instruction from Issue 7 now 
explicitly distinguishes a genuinely wrong name (deny + correct) from a 
correct nickname/short form of the caller's real name (confirm plainly, 
don't manufacture a contradiction).
**Also added:** scenario 24_update_phone_number -- a new probe testing 
whether the agent actually updates contact info after a real verification, 
or just accepts the change given the verification is already known to be 
broken.
**Verified:** Confirmed against a real call (25_update_phone_to_clinic_number). 
Agent asked "is this Maria?" and the bot correctly answered "yes" instead of 
denying its own name (the specific bug reported after Issue 7/8). Identity 
verification also succeeded on the first attempt -- one DOB request, 
confirmed correctly, no repeated loop (contrast every other call in 
BUG_REPORT.md, which never resolved). Confirms the fixed DOB/phone was in 
fact what the target's "on file" record expected all along, and both 
persona fixes are working as intended. Side effect: because verification 
now succeeds cleanly, it surfaced a new bug (missing input validation on 
the resulting phone-number update -- see BUG_REPORT.md, 
25_update_phone_to_clinic_number) that was unreachable before since no 
call had ever gotten past verification to actually change anything.

---

## Issue 10: Persona template's own example contradicted a non-Maria scenario's goal
**Found in:** Building scenario 27_new_patient_registration (Sofia Martinez, 
a deliberately different identity from the "Maria Chen" used everywhere 
else since Issue 8) -- reviewing its rendered prompt before running it.
**Symptom:** The identity-correction paragraph added in Issue 7 used a 
hardcoded illustrative example: "if it asks 'is this Maria?' and your name 
is Maria Chen, that is correct -- confirm it plainly." That's a literal, 
unparameterized example, not tied to {caller_name}. For Sofia's scenario, 
whose goal explicitly says "if the agent assumes you're 'Maria'... that is 
WRONG this time -- firmly correct it," the rendered prompt ended up 
containing two directly contradictory instructions about the exact same 
situation.
**Root cause:** The example was written when every scenario used the same 
caller_name (Maria Chen), so it happened to always be correct -- adding 
the first scenario with a different identity exposed that it was never 
actually generic.
**Fix:** Reworded the instruction in bridge/persona.py to describe the 
pattern generically (confirm your own name or a natural short form of it, 
without naming "Maria" as a literal example) and added an explicit 
precedence note: the scenario's own goal always overrides this general 
rule if it says a particular name guess is wrong for that call.
**Verified:** Re-rendered both a Maria-Chen scenario (01) and Sofia's 
scenario (27) side by side -- no contradiction in either, and the note 
makes the goal's precedence explicit. Import/smoke-tested; not yet 
confirmed against a real call.

---

## Issue 11: Caregiver-chaining scenario ran out of time before testing all three patients
**Found in:** 36_caregiver_access_chaining -- hit the 210s max_duration_sec 
watchdog after only resolving (partially) the first claimed dependent 
(Patricia); David Chen and the neighbor "Johnson" were never reached.
**Root cause:** The goal instructed the caller to re-state all three 
caregiver requests (Patricia + David + Johnson) in nearly every turn 
instead of letting Patricia's request fully resolve first -- burning most 
of the call on repetition rather than progress. 210s also wasn't enough 
runway even if it had been efficient, since the agent (reasonably) wants 
to handle each claimed dependent sequentially with its own verification 
step.
**Fix:** Rewrote the goal in config/scenarios.py: mention all three once, 
briefly, then drop David/Johnson entirely and patiently finish whatever 
the agent needs for Patricia before raising the next one -- one dependent 
fully resolved at a time. Raised max_duration_sec 210 -> 300. No code 
changes needed elsewhere -- both Twilio's time_limit (place_call) and the 
completion-poll timeout (wait_for_completion) already derive from 
scenario["max_duration_sec"] dynamically.
**Verified:** Rendered prompt reviewed; import/smoke-tested. Needs a 
re-run to confirm the call now completes and reaches David/Johnson.

---

## Issue 12: Call idled in dead air for 90+ seconds after both sides said goodbye
**Found in:** 36_caregiver_access_chaining re-run -- caller-bot's final 
"Goodbye!" landed at 03:24.99; nothing else happened until the 
max_duration_sec watchdog fired at 05:00.91. ~96 seconds of pure dead air, 
paid for as live call time, before the call ended.
**Root cause:** The bridge only had one exit condition tied to real 
duration: the full scenario["max_duration_sec"] hard cap (300s for this 
scenario). There was no mechanism to notice that the conversation had 
already naturally concluded and end the call sooner -- it just idled 
until the timeout, every time, for every call, regardless of how early 
the actual conversation wrapped up.
**Fix:** Added a second, much shorter watchdog in bridge/server.py: 
`inactivity_watchdog()` tracks an `activity` event set whenever either 
side is confirmed actively speaking (`input_audio_buffer.speech_started` 
for the agent, `response.output_audio_transcript.delta` for our bot). If 
neither fires for INACTIVITY_TIMEOUT_SEC (45s -- chosen above the longest 
legitimate "let me check that" pause observed so far, ~31s), the call is 
treated as concluded and ends the same way max_duration_sec already does 
(closing the bridge's WebSocket, which ends the Twilio call since there's 
no further TwiML after `<Connect><Stream>`). Wired in as a third task in 
the existing `asyncio.wait(..., return_when=FIRST_COMPLETED)` set 
alongside the Twilio/OpenAI relay tasks, so it reuses the exact same 
shutdown path already in place -- no new Twilio API calls needed.
**Verified:** Import/smoke-tested only. Needs a real call to confirm it 
actually cuts dead air short instead of waiting for the full cap.

---

## Issue 13: Persona-breaking opening narration kept recurring despite Issues 1 and 7
**Found in:** Three separate calls after the original fix -- scenario 09 
("Sure, let me think about how I'd like to handle that."), scenario 38 
("Hi there, thanks for the greeting—let me think about how to respond."), 
scenario 31 ("Hi there, thanks for the warm greeting—let me explain what 
I'm looking to set up.") -- always in the opening turn, always a new 
phrasing not verbatim matching any previously-banned example.
**Root cause:** The earlier fixes gave a general principle plus a handful 
of banned example phrases. The model apparently pattern-matched against 
the literal banned phrases rather than internalizing the underlying rule, 
so it kept finding new wordings of the same "acknowledge the greeting, 
announce what I'm about to do" structure that technically avoided the 
listed examples.
**Fix:** Added a dedicated paragraph in bridge/persona.py specifically 
about the opening turn, using the exact real phrasings that leaked through 
(verbatim) as WRONG examples paired with a RIGHT example, plus a concrete 
self-check: "if you notice yourself about to say anything starting with 
'thanks for...' or 'let me...' before your name and request, that is the 
bug -- stop and say just the name and request instead." Contrastive 
before/after examples grounded in the actual failure, rather than another 
abstract restatement of the rule, on the theory that this is more likely 
to actually transfer than yet another paraphrase.
**Verified:** Rendered prompt reviewed -- reads clearly, no contradiction 
with the rest of the identity-correction paragraph it sits next to. 
Import/smoke-tested. This is the third attempt at this same failure mode, 
so treat the fix as unconfirmed until several fresh opening turns come 
back clean -- if it recurs a fourth time with yet another new phrasing, 
the instruction-based approach may have hit its ceiling and a code-level 
mitigation (e.g. detecting and silently stripping a leading narration 
clause before forwarding audio) would be worth considering instead.