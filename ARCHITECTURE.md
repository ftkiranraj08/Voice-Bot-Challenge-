# Architecture

The bot places outbound calls through Twilio, which connects each call to a local
FastAPI server over a bidirectional [Media Stream](https://www.twilio.com/docs/voice/media-streams)
websocket (`/media-stream`). That server opens a matching websocket to OpenAI's
Realtime API (`bridge/realtime_client.py`), configured for `g711_ulaw` audio in
both directions -- the same format Twilio Media Streams uses natively -- so audio
is relayed as raw base64 passthrough with no resampling or transcoding in either
direction. The Realtime session plays the "caller" role: its system instructions
are a persona built from a single template (`bridge/persona.py`) parameterized by
a scenario config (`config/scenarios.py` -- name, goal, personality trait, and an
optional opening line), so adding a new test scenario is a data change, not a
code change. OpenAI's server-side VAD drives turn-taking natively, including
barge-in: when it detects the healthcare agent starting to talk, the bridge sends
Twilio a `clear` event to flush any of the caller bot's audio still queued for
playback, so interruptions resolve like a real call instead of both sides talking
over each other. Per-call transcripts are built live from the Realtime API's own
transcript events (`response.audio_transcript.*` for the caller bot,
`conversation.item.input_audio_transcription.completed` for the healthcare agent)
rather than a separate STT pass -- it's the same model that's already listening,
so there's no second transcription to drift out of sync with what was actually
said.

`run_calls.py` is the single orchestration entrypoint: it iterates the scenario
list, places each call via the Twilio REST API with `record=True` and a
`time_limit` slightly above the scenario's own cap (the bridge enforces the real
cap itself via an asyncio watchdog, so a stuck call can't balloon cost), polls
until the call reaches a terminal status, downloads the recording, and merges
call status/recording path into the metadata JSON the bridge already wrote when
the call ended. Cost is bounded by using `gpt-realtime-2.1-mini`, capping every
call at 2-3.5 minutes, and keeping the system prompt short and stable across
calls so repeated instruction tokens land in OpenAI's cached-input tier rather
than being billed at full price on every call.
