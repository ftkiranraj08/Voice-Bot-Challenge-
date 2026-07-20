"""Turn-by-turn transcript logging for a single call, timestamped from call start."""

import json
import os
import time

# gap_ms measures the healthcare agent's response latency to our bot. Turns
# under this are flagged in the summary as suspiciously snappy -- likely the
# agent barging in before our bot actually finished speaking.
SHORT_GAP_THRESHOLD_MS = 150

# gap_ms_audio is a cross-check on gap_ms: instead of trusting response.done's
# timestamp for "when did our bot finish talking", it's computed from the
# actual audio byte count generated (exact, since G.711 is a fixed-rate
# codec). If the two disagree by more than this, that's evidence Twilio's
# `clear` clipped audio that hadn't finished playing yet even though the
# model itself considered the response already done.
GAP_DIVERGENCE_THRESHOLD_MS = 300


class TranscriptLogger:
    def __init__(self, call_label, scenario):
        self.call_label = call_label
        self.scenario = scenario
        self.start_time = time.monotonic()
        self.start_wall_clock = time.time()
        self.lines = []  # (elapsed_sec, role, text, gap_ms, is_interrupt, gap_ms_audio)
        # in-progress streaming text per role, flushed on the matching *.done event
        self._pending = {"caller_bot": "", "healthcare_agent": ""}

    def _elapsed(self):
        return time.monotonic() - self.start_time

    def append_delta(self, role, text_delta):
        self._pending[role] += text_delta

    def flush(self, role):
        text = self._pending.get(role, "").strip()
        self._pending[role] = ""
        if text:
            self.lines.append((self._elapsed(), role, text, None, False, None))
        return text

    def add_line(self, role, text, gap_ms=None, is_interrupt=False, gap_ms_audio=None):
        text = text.strip()
        if text:
            self.lines.append((self._elapsed(), role, text, gap_ms, is_interrupt, gap_ms_audio))

    def add_event_marker(self, note):
        self.lines.append((self._elapsed(), "system", note, None, False, None))

    def write(self, transcripts_dir, metadata_dir, call_sid, extra_metadata=None):
        os.makedirs(transcripts_dir, exist_ok=True)
        os.makedirs(metadata_dir, exist_ok=True)

        txt_path = os.path.join(transcripts_dir, f"{self.call_label}.txt")
        with open(txt_path, "w") as f:
            f.write(f"Scenario: {self.scenario['name']} ({self.scenario['id']})\n")
            f.write(f"Caller persona: {self.scenario['caller_name']}\n")
            f.write(f"Call SID: {call_sid}\n")
            f.write(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.start_wall_clock))}\n")
            f.write("-" * 60 + "\n\n")

            gaps = []  # (elapsed_sec, gap_ms, is_interrupt, gap_ms_audio) for the turn-taking summary
            for elapsed, role, text, gap_ms, is_interrupt, gap_ms_audio in self.lines:
                mm = int(elapsed // 60)
                ss = elapsed % 60
                label = {
                    "caller_bot": "CALLER (bot)",
                    "healthcare_agent": "AGENT",
                    "system": "SYSTEM",
                }.get(role, role.upper())
                if gap_ms is None:
                    gap_note = ""
                else:
                    audio_part = f", audio-check: {gap_ms_audio}ms" if gap_ms_audio is not None else ""
                    tag_part = ", MID-SPEECH INTERRUPT" if is_interrupt else ""
                    gap_note = f"(gap: {gap_ms}ms{audio_part}{tag_part}) "
                f.write(f"[{mm:02d}:{ss:05.2f}] {label}: {gap_note}{text}\n")
                if role == "healthcare_agent" and gap_ms is not None:
                    gaps.append((elapsed, gap_ms, is_interrupt, gap_ms_audio))

            if gaps:
                avg_gap = sum(g for _, g, _, _ in gaps) / len(gaps)
                # A mid-speech interrupt is always flagged (it's the more
                # severe instance of the same issue); a merely short gap is
                # flagged only past the threshold.
                flagged = []
                for e, g, is_interrupt, _ in gaps:
                    if is_interrupt:
                        flagged.append((e, g, "MID-SPEECH INTERRUPT"))
                    elif g < SHORT_GAP_THRESHOLD_MS:
                        flagged.append((e, g, "SHORT GAP"))
                flagged.sort(key=lambda row: row[0])
                n_short = sum(1 for *_, tag in flagged if tag == "SHORT GAP")
                n_interrupt = sum(1 for *_, tag in flagged if tag == "MID-SPEECH INTERRUPT")

                # Divergence: turns where the event-based gap and the
                # audio-byte-based gap disagree by a lot -- a candidate sign
                # that `clear` clipped audio the model still considered live.
                diverged = [
                    (e, g, g_audio)
                    for e, g, _, g_audio in gaps
                    if g_audio is not None and abs(g - g_audio) > GAP_DIVERGENCE_THRESHOLD_MS
                ]

                f.write("\n" + "-" * 60 + "\n")
                f.write("Turn-taking summary:\n")
                f.write(f"  Turns measured: {len(gaps)}\n")
                f.write(f"  Average gap: {avg_gap:.0f}ms\n")
                f.write(f"  Flagged: {n_short} short gap(s), {n_interrupt} mid-speech interrupt(s)\n")
                for elapsed, gap_ms, tag in flagged:
                    mm = int(elapsed // 60)
                    ss = elapsed % 60
                    f.write(f"    [{mm:02d}:{ss:05.2f}] gap={gap_ms}ms {tag}\n")
                if diverged:
                    f.write(
                        f"  Gap-method disagreement (>{GAP_DIVERGENCE_THRESHOLD_MS}ms, "
                        f"event-based vs audio-byte-based): {len(diverged)}\n"
                    )
                    for elapsed, gap_ms, gap_ms_audio in diverged:
                        mm = int(elapsed // 60)
                        ss = elapsed % 60
                        f.write(
                            f"    [{mm:02d}:{ss:05.2f}] event-based={gap_ms}ms "
                            f"audio-based={gap_ms_audio}ms -- possible clipped audio\n"
                        )

        duration_sec = self._elapsed()
        meta_path = os.path.join(metadata_dir, f"{self.call_label}.json")
        metadata = {
            "call_label": self.call_label,
            "scenario_id": self.scenario["id"],
            "scenario_name": self.scenario["name"],
            "probe": self.scenario["probe"],
            "call_sid": call_sid,
            "started_at": self.start_wall_clock,
            "duration_sec": round(duration_sec, 1),
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        with open(meta_path, "w") as f:
            json.dump(metadata, f, indent=2)

        return txt_path, meta_path
