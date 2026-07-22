"""Thin async wrapper around the OpenAI Realtime API (GA) websocket.

Audio format is set to audio/pcmu (G.711 mu-law) on both directions so raw
Twilio Media Stream payloads can be forwarded to OpenAI (and responses
forwarded back to Twilio) with no resampling/transcoding -- just base64
passthrough.
"""

import base64
import json
import os
import time

# The top-level `websockets.connect` resolves to the legacy client, whose
# headers kwarg is `extra_headers`. Import the new asyncio client explicitly
# so `additional_headers` (and current-gen behavior generally) is used.
from websockets.asyncio.client import connect as ws_connect

OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"

# A fixed voice keeps delivery consistent across all calls/scenarios.
DEFAULT_VOICE = "alloy"

# G.711 mu-law @ 8kHz: 8000 samples/sec, 1 byte/sample -> 8000 bytes/sec.
# Used to convert audio delta byte counts into actual playback duration.
PCMU_BYTES_PER_SEC = 8000


class RealtimeClient:
    def __init__(self, instructions, model=None, api_key=None):
        self.instructions = instructions
        self.model = model or os.environ["OPENAI_REALTIME_MODEL"]
        self.api_key = api_key or os.environ["OPENAI_API_KEY"]
        self.ws = None

        # Turn-taking gap tracking: the HEALTHCARE AGENT's response latency
        # to us -- time from our bot's audio actually finishing PLAYBACK
        # (not just finishing generation) to the agent's speech resuming
        # (input_audio_buffer.speech_started, the same VAD signal the
        # barge-in flush logic uses).
        #
        # response.done fires when the model finishes GENERATING a response,
        # which can be well before Twilio finishes actually playing all of
        # that audio out over the phone line (generation can outrun
        # real-time playback). So instead of using response.done, we predict
        # the true playback-end time from the audio itself: G.711 mu-law is
        # a fixed 8000 bytes/sec, so summing each delta's byte length gives
        # an accurate running estimate of when the audio will finish
        # playing. `last_gap_ms` is (re)computed once per turn, as
        # (speech_started time - estimated playback-end time) -- negative
        # means the agent started talking before our audio actually
        # finished playing, i.e. a genuine mid-speech interrupt.
        self._playback_started_at = None
        self._estimated_stopped_at = None
        self._gap_captured_for_turn = True
        self.last_gap_ms = None
        self.last_gap_is_interrupt = False

    async def connect(self):
        # GA API (post 2026-05-12 beta shutdown): no OpenAI-Beta header, model
        # still goes in the URL query param.
        url = f"{OPENAI_REALTIME_URL}?model={self.model}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        self.ws = await ws_connect(
            url,
            additional_headers=headers,
            max_size=None,
        )
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": self.instructions,
                    "output_modalities": ["audio"],
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcmu"},
                            "transcription": {"model": "gpt-4o-transcribe"},
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": 0.5,
                                "prefix_padding_ms": 300,
                                "silence_duration_ms": 500,
                            },
                        },
                        "output": {
                            "voice": DEFAULT_VOICE,
                            "format": {"type": "audio/pcmu"},
                        },
                    },
                },
            }
        )

    async def _send(self, event):
        await self.ws.send(json.dumps(event))

    async def append_audio(self, mulaw_b64_payload):
        await self._send(
            {"type": "input_audio_buffer.append", "audio": mulaw_b64_payload}
        )

    async def trigger_response(self):
        """Ask the bot to speak first (used when it should open the call)."""
        await self._send({"type": "response.create"})

    async def cancel_response(self):
        """Stop the model mid-generation on barge-in.

        Without this, flushing Twilio's audio queue (the `clear` event) only
        stops what's already been sent from being played -- the model keeps
        generating and streaming more of its planned sentence regardless,
        which both bleeds through as audio the agent shouldn't be hearing
        and keeps extending the playback-end estimate used for gap timing.
        Safe to call even if no response is currently active (OpenAI just
        returns a harmless error event in that case).
        """
        await self._send({"type": "response.cancel"})

    def _track_turn_gap(self, event):
        etype = event.get("type")
        if etype == "response.created":
            # New response cycle -- reset the playback-end estimate so it
            # doesn't carry over from the previous turn.
            self._playback_started_at = None
            self._estimated_stopped_at = None
            self._gap_captured_for_turn = False

        elif etype == "response.output_audio.delta":
            try:
                n_bytes = len(base64.b64decode(event.get("delta", "")))
            except (ValueError, TypeError):
                n_bytes = 0
            duration_sec = n_bytes / PCMU_BYTES_PER_SEC
            now = time.monotonic()
            if self._playback_started_at is None:
                self._playback_started_at = now
                self._estimated_stopped_at = now + duration_sec
            else:
                self._estimated_stopped_at += duration_sec

        elif etype == "input_audio_buffer.speech_started" and not self._gap_captured_for_turn:
            self._gap_captured_for_turn = True
            now = time.monotonic()
            if self._estimated_stopped_at is not None:
                self.last_gap_ms = round((now - self._estimated_stopped_at) * 1000)
                self.last_gap_is_interrupt = self.last_gap_ms < 0
            else:
                self.last_gap_ms = None
                self.last_gap_is_interrupt = False

    async def events(self):
        async for raw in self.ws:
            event = json.loads(raw)
            self._track_turn_gap(event)
            yield event

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
