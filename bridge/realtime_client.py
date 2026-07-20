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

# G.711 (audio/pcmu) is a fixed-rate codec: 8000 samples/sec, 1 byte/sample --
# so audio duration in seconds is exactly byte_count / 8000, no estimation.
G711_BYTES_PER_SEC = 8000


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
        self._response_has_audio = False
        self._bot_stopped_at = None
        self._gap_captured_for_turn = True
        self.last_gap_ms = None
        self.last_gap_is_interrupt = False

        # Cross-check on last_gap_ms: instead of trusting response.done's
        # timestamp for "when did our bot finish talking", compute it from
        # the actual audio generated (exact, via G711_BYTES_PER_SEC) rather
        # than the model's own "I'm done" event. If the two disagree by a
        # lot, that's evidence Twilio's `clear` clipped audio that hadn't
        # finished playing yet even though the model considered itself done.
        self._response_audio_bytes = 0
        self._response_audio_started_at = None
        self.last_gap_ms_audio = None

        # gpt-realtime-2.1(-mini) can emit a response as two sequential
        # items: a 'commentary' item (the model's internal reasoning) and a
        # 'final_answer' item (what it actually means to say). Both were
        # being synthesized to audio and forwarded to the call, and both
        # got glued into the transcript with no separator -- Maria was
        # audibly narrating her own reasoning before every real answer.
        # item_id -> phase, populated from response.output_item.added.
        self._item_phases = {}

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
        """Stop the model from continuing to generate the in-flight response.

        Barge-in must do this in addition to flushing Twilio's playback
        buffer -- otherwise the model keeps composing (and we keep hearing
        about, via transcript deltas) a response that was already cut off on
        the audio side, so the transcript and the actual call diverge and
        the "response in progress" state never clears for gap tracking.

        Gated on the response actually having produced audio: a response
        that's been created but hasn't emitted a single audio delta yet has
        nothing audible to interrupt. Cancelling it anyway both mislabels a
        non-event as a barge-in and frequently races the server naturally
        finishing that (often near-empty) response first, surfacing as a
        benign but noisy "no active response found" error.
        """
        if self._response_in_progress and self._response_has_audio:
            await self._send({"type": "response.cancel"})

    def _track_item_phases(self, event):
        if event.get("type") == "response.output_item.added":
            item = event.get("item") or {}
            item_id = item.get("id")
            if item_id:
                self._item_phases[item_id] = item.get("phase")

    def is_commentary(self, event):
        """True if this event belongs to a 'commentary' (reasoning) item
        rather than the model's actual final_answer. Commentary is internal
        reasoning that should never be played to the caller or logged as
        something Maria said -- events with no known/tracked item_id (a
        phase field that was never present) are treated as NOT commentary,
        so we fail open rather than silently dropping real speech.
        """
        return self._item_phases.get(event.get("item_id")) == "commentary"

    def _track_turn_gap(self, event):
        etype = event.get("type")
        if etype == "response.created":
            # New response cycle -- arm gap capture for whatever happens
            # next, whether that's a mid-speech interrupt or (later) a
            # normal post-completion gap.
            self._response_started_at = time.monotonic()
            self._response_in_progress = True
            self._response_has_audio = False
            self._response_audio_bytes = 0
            self._response_audio_started_at = None
            self._gap_captured_for_turn = False
        elif etype == "response.output_audio.delta" and not self.is_commentary(event):
            self._response_has_audio = True
            if self._response_audio_started_at is None:
                self._response_audio_started_at = time.monotonic()
            delta = event.get("delta") or ""
            if delta:
                self._response_audio_bytes += len(base64.b64decode(delta))
        elif etype == "response.done":
            self._bot_stopped_at = time.monotonic()
            self._response_in_progress = False
        elif etype == "input_audio_buffer.speech_started" and not self._gap_captured_for_turn:
            self._gap_captured_for_turn = True
            now = time.monotonic()
            if self._response_in_progress and self._response_started_at is not None and self._response_has_audio:
                # Real mid-speech interrupt: our bot was audibly talking.
                # Audio total isn't final mid-response, so no cross-check here.
                self.last_gap_ms = round((self._response_started_at - now) * 1000)
                self.last_gap_is_interrupt = True
                self.last_gap_ms_audio = None
            elif not self._response_in_progress and self._bot_stopped_at is not None:
                self.last_gap_ms = round((now - self._bot_stopped_at) * 1000)
                self.last_gap_is_interrupt = False
                if self._response_audio_started_at is not None:
                    audio_duration_sec = self._response_audio_bytes / G711_BYTES_PER_SEC
                    audio_finished_at = self._response_audio_started_at + audio_duration_sec
                    self.last_gap_ms_audio = round((now - audio_finished_at) * 1000)
                else:
                    self.last_gap_ms_audio = None
            else:
                # Either nothing to measure yet, or a response is in flight
                # but hasn't produced audio -- not a real interrupt, and
                # using a stale bot_stopped_at here would misattribute the gap.
                self.last_gap_ms = None
                self.last_gap_is_interrupt = False
                self.last_gap_ms_audio = None

    async def events(self):
        async for raw in self.ws:
            event = json.loads(raw)
            self._track_item_phases(event)
            self._track_turn_gap(event)
            yield event

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
