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


def wait_for_recording(client, call_sid, poll_interval_sec=3, timeout_sec=90):
    """Poll until a recording exists AND Twilio marks it "completed".

    A recording resource shows up in the list as soon as it starts, but its
    media isn't guaranteed fetchable until post-call processing finishes and
    status flips to "completed" -- fetching the .mp3 before then 404s.
    """
    deadline = time.monotonic() + timeout_sec
    last_seen = None
    while time.monotonic() < deadline:
        recordings = client.recordings.list(call_sid=call_sid, limit=1)
        if recordings:
            last_seen = recordings[0]
            if last_seen.status == "completed":
                return last_seen
        time.sleep(poll_interval_sec)
    # Timed out waiting for "completed" -- hand back whatever we last saw
    # (if anything) so the caller can still try, rather than giving up outright.
    return last_seen


def download_recording(recording, dest_path, max_attempts=4, retry_delay_sec=5):
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Recordings/{recording.sid}.mp3"

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, auth=(account_sid, auth_token), timeout=30)
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                f.write(resp.content)
            return
        except requests.exceptions.HTTPError:
            if attempt == max_attempts:
                raise
            time.sleep(retry_delay_sec)
