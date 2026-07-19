# Healthcare AI voice agent tester

A voice bot that calls a target phone number, plays a "patient" persona from a
config-driven scenario list, and records a transcript + audio for each call so
the target's responses can be reviewed for bugs. See `ARCHITECTURE.md` for the
design.

## Prerequisites

- Python 3.9+ (a venv at `.venv` is already set up in this repo)
- A Twilio account with a phone number capable of outbound calls
- An OpenAI account with Realtime API access
- A public tunnel to your machine, e.g. [ngrok](https://ngrok.com/download)
  (`brew install ngrok`) -- Twilio needs to reach your local bridge server

## Setup

1. Install dependencies (already done if `.venv/` exists):
   ```
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

2. Fill in `.env` (copy `.env.example` if starting fresh). Required:
   - `OPENAI_API_KEY`, `OPENAI_REALTIME_MODEL` -- double check the model id
     against OpenAI's current realtime model list before your first run
   - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`
   - `TARGET_PHONE_NUMBER` -- already set to the challenge target, `+18054398008`
   - `PUBLIC_BASE_URL` -- your ngrok HTTPS URL (see step 3)

3. In one terminal, start a tunnel to the port the bridge server will use
   (default 8000), and copy the `https://...ngrok-free.app` URL it prints into
   `PUBLIC_BASE_URL` in `.env`:
   ```
   ngrok http 8000
   ```

4. In another terminal, start the bridge server:
   ```
   .venv/bin/python -m bridge.server
   ```

## Running

**Do a single test call first** to check turn-taking/pacing before spending on
the full batch:
```
.venv/bin/python run_calls.py --single 01_new_appointment
```
Listen to the resulting `recordings/01_new_appointment.mp3` and read
`transcripts/01_new_appointment.txt`. Iterate on `bridge/persona.py` or the
Realtime session config in `bridge/realtime_client.py` if pacing/interruptions
feel off, then re-run.

**Run the full scenario batch** (single command, runs sequentially and waits
for each call + its recording before moving on):
```
.venv/bin/python run_calls.py
```

List available scenario ids without placing any calls:
```
.venv/bin/python run_calls.py --list
```

## Output

Each call produces three files, all keyed by the same `call-XX_<scenario_id>` label:
- `transcripts/<call_label>.txt` -- turn-by-turn transcript with timestamps
- `recordings/<call_label>.mp3` -- full call audio, downloaded from Twilio
- `metadata/<call_label>.json` -- scenario info, call SID, status, duration

## Notes / gotchas

- The bridge server reads `.env` once at startup -- if you change `.env`
  (e.g. a new ngrok URL), restart it.
- ngrok's free tier gives you a new URL every time you restart it; update
  `PUBLIC_BASE_URL` accordingly.
- `run_calls.py` does a pre-flight check that it can reach your bridge
  server's `/twiml` endpoint through the tunnel before placing any calls, to
  avoid burning a call attempt on a misconfigured tunnel.
- Each call is hard-capped at its scenario's `max_duration_sec` (2-3.5 min) by
  an in-process watchdog in the bridge, independent of Twilio's own
  `time_limit` safety net, so a stuck call can't run away on cost.
