"""FastAPI bridge: Twilio Media Streams <-> OpenAI Realtime API.

Two endpoints:
  GET/POST /twiml          returns the TwiML that connects the call to /media-stream
  WS       /media-stream   bidirectional audio relay + transcript logging for one call

Audio never leaves mulaw/8kHz -- Twilio sends/receives g711 mulaw base64 chunks
and the Realtime session is configured for audio/pcmu in and out, so relaying is
just base64 passthrough between the two websockets.
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from config.scenarios import get_scenario  # noqa: E402
from bridge.persona import build_instructions  # noqa: E402
from bridge.realtime_client import RealtimeClient  # noqa: E402
from bridge.transcript import TranscriptLogger  # noqa: E402

app = FastAPI()

TRANSCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "transcripts")
METADATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "metadata")

# If neither side has produced any audio this long after connecting, nudge the
# caller bot to speak first rather than sitting in silence for the whole call.
OPENING_GRACE_SEC = 5.0

# If neither side has actively spoken for this long (as opposed to a normal
# "let me check that" pause, the longest of which observed so far is ~30s),
# treat the call as naturally concluded and end it rather than idling all
# the way out to max_duration_sec -- a real call was seen sitting in dead
# air for 90+ seconds after both sides had already said goodbye.
INACTIVITY_TIMEOUT_SEC = 45.0

# Expected/harmless realtime API error codes that shouldn't clutter the
# transcript. response_cancel_not_active fires every time we call
# cancel_response() on a speech_started that isn't actually interrupting an
# in-flight bot response (i.e. most agent turns) -- by design, not a bug.
HARMLESS_ERROR_CODES = {"response_cancel_not_active"}


@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml(request: Request):
    scenario_id = request.query_params.get("scenario_id")
    call_label = request.query_params.get("call_label", scenario_id or "call")
    base_url = os.environ["PUBLIC_BASE_URL"].replace("https://", "").replace("http://", "")

    twiml_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{base_url}/media-stream">
      <Parameter name="scenario_id" value="{scenario_id}"/>
      <Parameter name="call_label" value="{call_label}"/>
    </Stream>
  </Connect>
</Response>"""
    return Response(content=twiml_xml, media_type="text/xml")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()

    stream_sid = None
    call_sid = None
    scenario = None
    transcript = None
    realtime = None
    heard_any_audio = asyncio.Event()
    activity = asyncio.Event()
    activity.set()  # grace period before the first real exchange happens

    try:
        # First frames: "connected" then "start" carry the info we need.
        while scenario is None:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg["event"] == "start":
                stream_sid = msg["start"]["streamSid"]
                call_sid = msg["start"]["callSid"]
                params = msg["start"].get("customParameters", {})
                scenario = get_scenario(params["scenario_id"])
                call_label = params.get("call_label", scenario["id"])
                transcript = TranscriptLogger(call_label, scenario)
                transcript.add_event_marker(f"Call started (streamSid={stream_sid}, callSid={call_sid})")

        instructions = build_instructions(scenario)
        realtime = RealtimeClient(instructions=instructions)
        await realtime.connect()
        # Captured on speech_started, consumed on the next AGENT transcript line.
        pending_gap_ms = None
        pending_gap_is_interrupt = False

        async def opening_watchdog():
            try:
                await asyncio.wait_for(heard_any_audio.wait(), timeout=OPENING_GRACE_SEC)
            except asyncio.TimeoutError:
                transcript.add_event_marker("No audio heard from agent -- nudging caller bot to open")
                await realtime.trigger_response()

        async def inactivity_watchdog():
            while True:
                try:
                    await asyncio.wait_for(activity.wait(), timeout=INACTIVITY_TIMEOUT_SEC)
                    activity.clear()
                except asyncio.TimeoutError:
                    transcript.add_event_marker(
                        f"No activity from either side for {INACTIVITY_TIMEOUT_SEC}s -- "
                        "treating the call as concluded"
                    )
                    return

        async def twilio_to_openai():
            async for raw in websocket.iter_text():
                msg = json.loads(raw)
                event = msg.get("event")
                if event == "media":
                    heard_any_audio.set()
                    await realtime.append_audio(msg["media"]["payload"])
                elif event == "stop":
                    transcript.add_event_marker("Twilio sent stop event")
                    break

        async def openai_to_twilio():
            nonlocal pending_gap_ms, pending_gap_is_interrupt
            async for event in realtime.events():
                etype = event.get("type")

                if etype == "response.output_audio.delta":
                    await websocket.send_text(
                        json.dumps(
                            {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": event["delta"]},
                            }
                        )
                    )

                elif etype == "input_audio_buffer.speech_started":
                    activity.set()
                    # Barge-in: the healthcare agent started talking.
                    # Flush whatever of our bot's audio Twilio still has
                    # queued so it stops immediately instead of talking
                    # over them...
                    await websocket.send_text(
                        json.dumps({"event": "clear", "streamSid": stream_sid})
                    )
                    # ...and tell OpenAI to actually stop generating that
                    # response too. Without this the model keeps producing
                    # the rest of its planned sentence regardless -- more
                    # audio bleeding through, more transcript text that
                    # never gets a clean place to land, and a playback-end
                    # estimate that keeps growing instead of stopping at
                    # the true interrupt point.
                    await realtime.cancel_response()

                    # Same signal is the agent's response-latency endpoint --
                    # RealtimeClient already computed the gap against its
                    # estimated true playback-end time (negative if this is
                    # a genuine mid-speech interrupt).
                    pending_gap_ms = realtime.last_gap_ms
                    pending_gap_is_interrupt = realtime.last_gap_is_interrupt

                    # Whatever the bot actually got out before being cut off
                    # goes in the transcript now -- don't wait on a
                    # response.output_audio_transcript.done that a cancelled
                    # response may never cleanly send.
                    transcript.flush("caller_bot")

                elif etype in ("response.created", "response.done"):
                    # Playback-end estimate is tracked internally by
                    # RealtimeClient off of response.created/output_audio.delta;
                    # nothing to do here.
                    pass

                elif etype == "response.output_audio_transcript.delta":
                    activity.set()
                    transcript.append_delta("caller_bot", event.get("delta", ""))
                elif etype == "response.output_audio_transcript.done":
                    transcript.flush("caller_bot")

                elif etype == "conversation.item.input_audio_transcription.completed":
                    transcript.add_line(
                        "healthcare_agent",
                        event.get("transcript", ""),
                        gap_ms=pending_gap_ms,
                        is_interrupt=pending_gap_is_interrupt,
                    )
                    pending_gap_ms = None
                    pending_gap_is_interrupt = False

                elif etype == "error":
                    error_code = (event.get("error") or {}).get("code")
                    if error_code not in HARMLESS_ERROR_CODES:
                        transcript.add_event_marker(f"OpenAI realtime error: {event.get('error')}")

                elif etype not in ("session.created", "session.updated"):
                    # GA event names are a recent migration (beta shutdown
                    # 2026-05-12) -- log anything we don't recognize so a
                    # renamed/unexpected event is easy to spot and wire up
                    # instead of silently vanishing.
                    print(f"[unhandled realtime event] {etype}: {event}")

        watchdog_task = asyncio.create_task(opening_watchdog())
        twilio_task = asyncio.create_task(twilio_to_openai())
        openai_task = asyncio.create_task(openai_to_twilio())
        inactivity_task = asyncio.create_task(inactivity_watchdog())
        try:
            # Stop as soon as EITHER side ends (e.g. Twilio's "stop" event)
            # OR the inactivity watchdog decides the call has naturally
            # concluded, rather than waiting for all of them -- otherwise a
            # call that's already over would sit idle until the hard
            # max_duration_sec timeout instead of finalizing right away.
            done, pending = await asyncio.wait(
                {twilio_task, openai_task, inactivity_task},
                timeout=scenario["max_duration_sec"],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                transcript.add_event_marker(f"Hit max_duration_sec watchdog ({scenario['max_duration_sec']}s)")
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for task in done:
                exc = task.exception()
                if exc:
                    transcript.add_event_marker(f"Relay task ended with error: {exc}")
        finally:
            watchdog_task.cancel()

    except WebSocketDisconnect:
        if transcript:
            transcript.add_event_marker("Twilio websocket disconnected")
    finally:
        if realtime is not None and realtime.ws is not None:
            await realtime.close()
        if transcript:
            transcript.write(TRANSCRIPTS_DIR, METADATA_DIR, call_sid)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
