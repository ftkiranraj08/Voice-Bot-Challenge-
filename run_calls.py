"""Single entrypoint: places calls for each scenario, waits for completion,
downloads the recording, and merges call status into that call's metadata
file (the bridge server already wrote the transcript + base metadata).

Usage:
  python run_calls.py                 # run every scenario in config/scenarios.py
  python run_calls.py --single 01_new_appointment   # run just one scenario (for iterating)
  python run_calls.py --from 03_cancel_appointment  # run this scenario through the end of the list
  python run_calls.py --list          # print scenario ids and exit
"""

import argparse
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

from config.scenarios import SCENARIOS, get_scenario  # noqa: E402
import twilio_client  # noqa: E402

RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
METADATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata")

DELAY_BETWEEN_CALLS_SEC = 8


def preflight_check():
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        sys.exit("PUBLIC_BASE_URL is not set in .env -- point it at your public tunnel (e.g. ngrok) URL.")
    try:
        resp = requests.get(f"{base_url}/twiml", params={"scenario_id": "01_new_appointment", "call_label": "healthcheck"}, timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        sys.exit(
            f"Could not reach bridge server at {base_url}/twiml ({exc}).\n"
            "Start it with `python -m bridge.server` (and make sure your tunnel is pointed at it) before running calls."
        )


def run_one_call(client, scenario, call_label):
    print(f"\n=== {call_label}: {scenario['name']} ===")
    call_sid = twilio_client.place_call(client, scenario, call_label)
    print(f"  placed call {call_sid}, waiting for completion...")

    status = twilio_client.wait_for_completion(
        client, call_sid, overall_timeout_sec=scenario["max_duration_sec"] + 90
    )
    print(f"  call status: {status}")

    recording_path = None
    recording_error = None
    try:
        recording = twilio_client.wait_for_recording(client, call_sid)
        if recording:
            os.makedirs(RECORDINGS_DIR, exist_ok=True)
            candidate_path = os.path.join(RECORDINGS_DIR, f"{call_label}.mp3")
            twilio_client.download_recording(recording, candidate_path)
            recording_path = candidate_path
            print(f"  saved recording -> {recording_path}")
        else:
            recording_error = "no recording found within timeout"
            print(f"  WARNING: {recording_error}")
    except Exception as exc:
        # A flaky recording download shouldn't take down the rest of the
        # batch -- log it, leave recording_path unset, and move on. The
        # transcript for this call is unaffected either way.
        recording_error = str(exc)
        print(f"  WARNING: recording download failed: {recording_error}")

    _merge_metadata(
        call_label,
        {"call_status": status, "recording_path": recording_path, "recording_error": recording_error},
    )


def _merge_metadata(call_label, extra):
    path = os.path.join(METADATA_DIR, f"{call_label}.json")
    data = {}
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
    data.update(extra)
    os.makedirs(METADATA_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--single", help="run only this scenario id")
    parser.add_argument("--from", dest="from_id", help="run this scenario id through the end of the list")
    parser.add_argument("--list", action="store_true", help="list scenario ids and exit")
    args = parser.parse_args()

    if args.list:
        for s in SCENARIOS:
            print(f"{s['id']:35s} {s['name']}")
        return

    preflight_check()
    client = twilio_client.get_client()

    if args.single:
        scenario = get_scenario(args.single)
        run_one_call(client, scenario, scenario["id"])
        return

    numbered_scenarios = list(enumerate(SCENARIOS, start=1))
    if args.from_id:
        start_idx = next((i for i, s in enumerate(SCENARIOS) if s["id"] == args.from_id), None)
        if start_idx is None:
            sys.exit(f"Unknown scenario id: {args.from_id}")
        numbered_scenarios = numbered_scenarios[start_idx:]

    for idx, scenario in numbered_scenarios:
        call_label = f"call-{idx:02d}_{scenario['id']}"
        run_one_call(client, scenario, call_label)
        if (idx, scenario) != numbered_scenarios[-1]:
            time.sleep(DELAY_BETWEEN_CALLS_SEC)

    print(f"\nDone. {len(numbered_scenarios)} calls placed. See transcripts/, recordings/, metadata/.")


if __name__ == "__main__":
    main()
