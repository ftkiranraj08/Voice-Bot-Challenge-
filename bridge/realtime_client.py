"""Thin async wrapper around the OpenAI Realtime API (GA) websocket.

Audio format is set to audio/pcmu (G.711 mu-law) on both directions so raw
Twilio Media Stream payloads can be forwarded to OpenAI (and responses
forwarded back to Twilio) with no resampling/transcoding -- just base64
passthrough.
"""

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


class RealtimeClient:
    def __init__(self, instructions, model=None, api_key=None):
        self.instructions = instructions
        self.model = model or os.environ["OPENAI_REALTIME_MODEL"]
        self.api_key = api_key or os.environ["OPENAI_API_KEY"]
        self.ws = None

        # Turn-taking gap tracking: the HEALTHCARE AGENT's response latency
        # to us -- time from our bot's response finishing (response.done) to
        # the agent's speech resuming (input_audio_buffer.speech_started,
        # the same VAD signal the barge-in flush logic uses). `last_gap_ms`
        # is (re)computed once per turn; callers just read it.
        #
        # If speech_started instead fires WHILE our response is still in
        # flight (response.created seen, response.done not yet), that's a
        # true mid-speech barge-in rather than a post-completion gap -- there
        # is no bot_stopped_at yet, so the gap is measured from when our
        # response started instead, which comes out negative.
        self._response_started_at = None
        self._response_in_progress = False
        self._bot_stopped_at = None
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

    def _track_turn_gap(self, event):
        etype = event.get("type")
        if etype == "response.created":
            # New response cycle -- arm gap capture for whatever happens
            # next, whether that's a mid-speech interrupt or (later) a
            # normal post-completion gap.
            self._response_started_at = time.monotonic()
            self._response_in_progress = True
            self._gap_captured_for_turn = False
        elif etype == "response.done":
            self._bot_stopped_at = time.monotonic()
            self._response_in_progress = False
        elif etype == "input_audio_buffer.speech_started" and not self._gap_captured_for_turn:
            self._gap_captured_for_turn = True
            now = time.monotonic()
            if self._response_in_progress and self._response_started_at is not None:
                self.last_gap_ms = round((self._response_started_at - now) * 1000)
                self.last_gap_is_interrupt = True
            elif self._bot_stopped_at is not None:
                self.last_gap_ms = round((now - self._bot_stopped_at) * 1000)
                self.last_gap_is_interrupt = False
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
