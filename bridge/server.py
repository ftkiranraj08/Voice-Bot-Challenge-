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

# After Twilio ends the media stream, give the OpenAI side this long to finish
# delivering any trailing events already in flight (e.g. a transcription for
# the agent's last utterance) before tearing the connection down, so the end
# of the call doesn't get silently dropped from the transcript.
TRAILING_EVENTS_GRACE_SEC = 2.5


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
        pending_gap_ms_audio = None

        async def opening_watchdog():
            try:
                await asyncio.wait_for(heard_any_audio.wait(), timeout=OPENING_GRACE_SEC)
            except asyncio.TimeoutError:
                transcript.add_event_marker("No audio heard from agent -- nudging caller bot to open")
                await realtime.trigger_response()

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
            nonlocal pending_gap_ms, pending_gap_is_interrupt, pending_gap_ms_audio
            async for event in realtime.events():
                etype = event.get("type")

                if etype == "response.output_audio.delta":
                    # Skip 'commentary' (reasoning) items entirely -- only
                    # the model's final_answer should ever reach the call.
                    if not realtime.is_commentary(event):
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
                    # Barge-in: the healthcare agent started talking. Flush
                    # whatever of our bot's audio Twilio still has queued so
                    # it stops immediately instead of talking over them, AND
                    # tell the model itself to stop generating that response
                    # -- flushing Twilio alone leaves the model composing (and
                    # us transcribing) a turn that was already cut off on the
                    # audio side, so the transcript stops matching what was
                    # actually said and "response in progress" never clears.
                    await websocket.send_text(
                        json.dumps({"event": "clear", "streamSid": stream_sid})
                    )
                    await realtime.cancel_response()
                    # Same signal is the agent's response-latency endpoint --
                    # RealtimeClient already computed the gap (negative if
                    # this is a true mid-speech interrupt, i.e. our response
                    # was still in flight) from right now.
                    pending_gap_ms = realtime.last_gap_ms
                    pending_gap_is_interrupt = realtime.last_gap_is_interrupt
                    pending_gap_ms_audio = realtime.last_gap_ms_audio

                elif etype == "response.created":
                    # Gap timing is handled internally by RealtimeClient.
                    pass

                elif etype == "response.done":
                    # Safety net: normally response.output_audio_transcript.done
                    # flushes whatever the caller-bot said. If a response gets
                    # cancelled mid-generation, that event may never fire for
                    # it -- without this, any partial sentence our bot actually
                    # spoke before being cut off would silently vanish from the
                    # transcript instead of showing up truncated. flush() is a
                    # no-op if there's nothing pending.
                    transcript.flush("caller_bot")

                elif etype == "response.output_audio_transcript.delta":
                    if not realtime.is_commentary(event):
                        transcript.append_delta("caller_bot", event.get("delta", ""))
                elif etype == "response.output_audio_transcript.done":
                    if not realtime.is_commentary(event):
                        transcript.flush("caller_bot")

                elif etype == "conversation.item.input_audio_transcription.completed":
                    transcript.add_line(
                        "healthcare_agent",
                        event.get("transcript", ""),
                        gap_ms=pending_gap_ms,
                        is_interrupt=pending_gap_is_interrupt,
                        gap_ms_audio=pending_gap_ms_audio,
                    )
                    pending_gap_ms = None
                    pending_gap_is_interrupt = False
                    pending_gap_ms_audio = None

                elif etype == "error":
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
        try:
            # Stop as soon as EITHER side ends (e.g. Twilio's "stop" event),
            # rather than waiting for both -- otherwise a call that already
            # hung up would sit here until the watchdog timeout instead of
            # finalizing right away.
            done, pending = await asyncio.wait(
                {twilio_task, openai_task},
                timeout=scenario["max_duration_sec"],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                transcript.add_event_marker(f"Hit max_duration_sec watchdog ({scenario['max_duration_sec']}s)")
            elif twilio_task in done and openai_task in pending:
                # Twilio ended the stream cleanly -- give OpenAI's trailing
                # events a brief window to arrive instead of cutting them off.
                trailing_done, trailing_pending = await asyncio.wait(
                    {openai_task}, timeout=TRAILING_EVENTS_GRACE_SEC
                )
                done |= trailing_done
                pending = trailing_pending
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
