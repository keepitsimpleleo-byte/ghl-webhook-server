#!/usr/bin/env python3
"""
One-time setup script: configure the GHL calendar for fixed time slots,
disable Google Calendar invitation emails, and register the appointment-booked webhook.

Usage:
  export GHL_API_KEY="..."
  export GHL_LOCATION_ID="..."
  export GHL_CALENDAR_ID="..."          # optional — defaults to WcMuX2qHzZeszRG4AzTM
  export WEBHOOK_BASE_URL="https://..."  # optional — if set, registers the webhook automatically

  python configure_calendar.py
"""

import os
import sys
import requests

API_KEY     = os.environ.get("GHL_API_KEY")
LOCATION_ID = os.environ.get("GHL_LOCATION_ID")
CALENDAR_ID = os.environ.get("GHL_CALENDAR_ID", "WcMuX2qHzZeszRG4AzTM")
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "").rstrip("/")

if not API_KEY:
    sys.exit("GHL_API_KEY not set")
if not LOCATION_ID:
    sys.exit("GHL_LOCATION_ID not set")

BASE_URL = "https://services.leadconnectorhq.com"
HEADERS  = {
    "Authorization": f"Bearer {API_KEY}",
    "Version":       "2021-07-28",
    "Content-Type":  "application/json",
}

# GHL requires one entry per day (daysOfTheWeek must be a single-element array)
# Mon=1 … Sat=6 (0=Sun)
WORKING_DAYS = [1, 2, 3, 4, 5, 6]

THREE_WINDOWS = [
    {"openHour": 8,  "openMinute": 0, "closeHour": 9,  "closeMinute": 0},
    {"openHour": 12, "openMinute": 0, "closeHour": 13, "closeMinute": 0},
    {"openHour": 15, "openMinute": 0, "closeHour": 16, "closeMinute": 0},
]


def patch_calendar():
    """Set 3 fixed 1-hour windows and disable Google Calendar invitation emails."""
    payload = {
        "slotDuration":           60,
        "slotInterval":           60,
        "preBuffer":              30,
        "preBufferUnit":          "mins",
        "slotBuffer":             30,
        "slotBufferUnit":         "mins",
        "googleInvitationEmails": False,
        "openHours": [
            {"daysOfTheWeek": [d], "hours": THREE_WINDOWS}
            for d in WORKING_DAYS
        ],
    }
    resp = requests.put(
        f"{BASE_URL}/calendars/{CALENDAR_ID}",
        headers=HEADERS,
        json=payload,
    )
    if resp.status_code in (200, 201):
        print("Calendar updated — 3 fixed slots (8am, 12pm, 3pm), invitation emails disabled.")
    else:
        print(f"Calendar PUT failed: {resp.status_code}")
        print(resp.text)
        print()
        print("Manual fallback — do this in the GHL dashboard:")
        print("  1. Calendars > Edit > Availability > Custom Hours")
        print("     Set time windows: 8:00–9:00, 12:00–13:00, 15:00–16:00")
        print("  2. Calendars > Edit > Notifications")
        print("     Uncheck 'Send Google Calendar Invite to Guest'")


def register_webhook():
    """Subscribe to AppointmentCreated so /appointment-booked fires on every new booking."""
    if not WEBHOOK_BASE_URL:
        print()
        print("WEBHOOK_BASE_URL not set — skipping automatic webhook registration.")
        print("To register manually in GHL:")
        print("  Settings > Integrations > Webhooks > Add Webhook")
        print("  Event: AppointmentCreated")
        print("  URL: https://your-server.com/appointment-booked")
        return

    url = f"{WEBHOOK_BASE_URL}/appointment-booked"
    payload = {
        "name":   "appointment-booked",
        "url":    url,
        "events": ["AppointmentCreated"],
    }
    resp = requests.post(
        f"{BASE_URL}/locations/{LOCATION_ID}/webhooks",
        headers=HEADERS,
        json=payload,
    )
    if resp.status_code in (200, 201):
        print(f"Webhook registered: {url}")
    else:
        print(f"Webhook registration failed: {resp.status_code}")
        print(resp.text)
        print(f"Register manually in GHL: Settings > Integrations > Webhooks")
        print(f"  Event: AppointmentCreated  URL: {url}")


if __name__ == "__main__":
    patch_calendar()
    register_webhook()
    print()
    print("Done. Restart webhook_server.py to pick up the new /appointment-booked route.")
