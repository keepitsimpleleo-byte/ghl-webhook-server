#!/usr/bin/env python3
"""
Background GHL health monitor — run every 30 min via cron.
Checks /health on the deployed server and sends a Twilio SMS when status
changes (ok → error or error → ok). Silent when nothing changes.

Cron entry:
  */30 * * * * cd "/Users/leomendoza/claude skills workspace/ghl-webhook-server" && python3 monitor_health.py >> /tmp/ghl-health-monitor.log 2>&1

Required env vars (set in your shell profile or .env):
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, GHL_OWNER_PHONE
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

HEALTH_URL = os.environ.get("RENDER_HEALTH_URL", "https://ghl-webhook-server-62c2.onrender.com/health")
STATE_FILE = Path.home() / ".ghl-health-state.json"
OWNER_PHONE = os.environ.get("GHL_OWNER_PHONE", "+17252968281")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"ghl": "unknown"}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def send_sms(message):
    sid   = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_ = os.environ.get("TWILIO_PHONE_NUMBER", "")
    if not all([sid, token, from_]):
        log.warning("Twilio credentials not set — cannot send SMS alert.")
        return
    try:
        from twilio.rest import Client
        Client(sid, token).messages.create(to=OWNER_PHONE, from_=from_, body=message)
        log.info(f"SMS sent to {OWNER_PHONE}: {message[:60]}")
    except Exception as e:
        log.error(f"SMS failed: {e}")


def check_health():
    try:
        resp = requests.get(HEALTH_URL, timeout=15)
        if resp.status_code != 200:
            return "error", f"HTTP {resp.status_code}"
        data = resp.json()
        ghl = data.get("ghl", "unknown")
        reason = data.get("reason", "")
        return ghl, reason
    except Exception as e:
        return "error", str(e)


def main():
    last = load_state()
    last_status = last.get("ghl", "unknown")

    current_status, reason = check_health()
    log.info(f"Health check: ghl={current_status}" + (f" | {reason}" if reason else ""))

    if current_status == last_status:
        # No change — stay silent
        return

    # Status changed — notify
    if current_status != "ok":
        msg = (
            f"⚠️ GHL server health degraded!\n"
            f"Status: {current_status}\n"
            f"{reason or 'Check Render logs for details.'}\n"
            f"URL: {HEALTH_URL}"
        )
        log.error(f"GHL health degraded: {reason}")
        send_sms(msg)
    else:
        msg = f"✅ GHL server recovered — all dependencies healthy."
        log.info("GHL health recovered.")
        send_sms(msg)

    save_state({"ghl": current_status, "updated": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    main()
