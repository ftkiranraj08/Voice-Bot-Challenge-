"""Twilio call placement, status polling, and recording download."""

import os
import time

import requests
from twilio.rest import Client

# Give Twilio's own hangup a little headroom over our scenario's max_duration_sec
# so the bridge's watchdog fires first in the normal case.
TIME_LIMIT_PADDING_SEC = 20


def get_client():
    return Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])


def place_call(client, scenario, call_label):
    base_url = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    twiml_url = f"{base_url}/twiml?scenario_id={scenario['id']}&call_label={call_label}"

    call = client.calls.create(
        to=os.environ["TARGET_PHONE_NUMBER"],
        from_=os.environ["TWILIO_PHONE_NUMBER"],
        url=twiml_url,
        method="GET",
        record=True,
        time_limit=scenario["max_duration_sec"] + TIME_LIMIT_PADDING_SEC,
    )
    return call.sid


TERMINAL_STATUSES = {"completed", "busy", "no-answer", "failed", "canceled"}


def wait_for_completion(client, call_sid, poll_interval_sec=5, overall_timeout_sec=600):
    deadline = time.monotonic() + overall_timeout_sec
    while time.monotonic() < deadline:
        call = client.calls(call_sid).fetch()
        if call.status in TERMINAL_STATUSES:
            return call.status
        time.sleep(poll_interval_sec)
    return "timeout"


RECORDING_FAILURE_STATUSES = {"failed", "absent"}


def wait_for_recording(client, call_sid, poll_interval_sec=3, timeout_sec=90):
    # Twilio lists the recording resource (status "processing") right after
    # the call ends, well before the .mp3 is actually fetchable -- downloading
    # then 404s. Wait for status "completed" specifically.
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        recordings = client.recordings.list(call_sid=call_sid, limit=1)
        if recordings:
            recording = recordings[0]
            if recording.status == "completed":
                return recording
            if recording.status in RECORDING_FAILURE_STATUSES:
                return None
        time.sleep(poll_interval_sec)
    return None


def download_recording(recording, dest_path):
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Recordings/{recording.sid}.mp3"
    resp = requests.get(url, auth=(account_sid, auth_token), timeout=30)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
