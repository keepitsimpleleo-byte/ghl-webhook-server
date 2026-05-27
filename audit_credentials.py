#!/usr/bin/env python3
"""
Full credential audit — tests every service credential against its live API.
Run anytime to confirm what's working after a key rotation or config change.

Usage:
    python3 audit_credentials.py

Reads credentials from environment variables. GHL creds fall back to
main-site-deploy/.env if not set in the environment.
"""

import os
import sys
import json
import requests
from pathlib import Path

# ── Load .env fallback for GHL creds ─────────────────────────────────────────
_env_path = Path(__file__).parent.parent / "main-site-deploy" / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

# ── Credential values ─────────────────────────────────────────────────────────
GHL_API_KEY     = os.environ.get("GHL_API_KEY", "")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID", "")
GHL_CALENDAR_ID = os.environ.get("GHL_CALENDAR_ID", "WcMuX2qHzZeszRG4AzTM")
GHL_OWNER_PHONE = os.environ.get("GHL_OWNER_PHONE", "+17252968281")
TWILIO_SID      = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN    = os.environ.get("TWILIO_AUTH_TOKEN", "")
NETLIFY_PAT     = os.environ.get("NETLIFY_PAT", "")
EL_API_KEY      = os.environ.get("ELEVENLABS_API_KEY", "")
EL_AGENT_ID     = os.environ.get("ELEVENLABS_AGENT_ID", "")
RENDER_URL      = os.environ.get("RENDER_HEALTH_URL",
                                  "https://ghl-webhook-server-62c2.onrender.com/health")

GHL_BASE = "https://services.leadconnectorhq.com"

results = []

def check(label, passed, detail=""):
    icon = "✅" if passed else "❌"
    results.append((icon, label, detail))
    print(f"  {icon}  {label}" + (f"  —  {detail}" if detail else ""))


# ── 1. GHL API key + location ─────────────────────────────────────────────────
print("\n── GHL ──────────────────────────────────────────────────────────────────")
if not GHL_API_KEY:
    check("GHL API key", False, "GHL_API_KEY not set")
else:
    headers = {"Authorization": f"Bearer {GHL_API_KEY}",
               "Version": "2021-07-28", "Content-Type": "application/json"}
    try:
        r = requests.get(f"{GHL_BASE}/contacts/", headers=headers,
                         params={"locationId": GHL_LOCATION_ID, "limit": 1}, timeout=10)
        check("GHL API key + location", r.status_code == 200,
              f"HTTP {r.status_code}" if r.status_code != 200 else f"location {GHL_LOCATION_ID}")
    except Exception as e:
        check("GHL API key + location", False, str(e))

    # Calendar ID
    try:
        r2 = requests.get(f"{GHL_BASE}/calendars/", headers=headers,
                          params={"locationId": GHL_LOCATION_ID}, timeout=10)
        if r2.status_code == 200:
            cal_ids = [c.get("id") for c in r2.json().get("calendars", [])]
            check("GHL calendar ID", GHL_CALENDAR_ID in cal_ids,
                  f"{GHL_CALENDAR_ID}" if GHL_CALENDAR_ID in cal_ids
                  else f"{GHL_CALENDAR_ID} NOT found — update GHL_CALENDAR_ID")
        else:
            check("GHL calendar ID", False, f"HTTP {r2.status_code}")
    except Exception as e:
        check("GHL calendar ID", False, str(e))

    # Owner contact
    try:
        r3 = requests.get(f"{GHL_BASE}/contacts/", headers=headers,
                          params={"locationId": GHL_LOCATION_ID, "query": GHL_OWNER_PHONE},
                          timeout=10)
        contacts = r3.json().get("contacts", []) if r3.status_code == 200 else []
        if contacts:
            name = f"{contacts[0].get('firstName','')} {contacts[0].get('lastName','')}".strip()
            check("GHL owner contact", True, f"{name} ({GHL_OWNER_PHONE})")
        else:
            check("GHL owner contact", False,
                  f"no contact found for {GHL_OWNER_PHONE} — run lookup_owner_contact.py")
    except Exception as e:
        check("GHL owner contact", False, str(e))


# ── 2. Twilio ─────────────────────────────────────────────────────────────────
print("\n── Twilio ───────────────────────────────────────────────────────────────")
if not TWILIO_SID or not TWILIO_TOKEN:
    check("Twilio credentials", False, "TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set")
else:
    try:
        r = requests.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}.json",
            auth=(TWILIO_SID, TWILIO_TOKEN), timeout=10)
        if r.status_code == 200:
            friendly = r.json().get("friendly_name", TWILIO_SID)
            check("Twilio SID + Auth Token", True, friendly)
        else:
            check("Twilio SID + Auth Token", False,
                  f"HTTP {r.status_code} — rotate TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN")
    except Exception as e:
        check("Twilio SID + Auth Token", False, str(e))


# ── 3. Netlify PAT ────────────────────────────────────────────────────────────
print("\n── Netlify ──────────────────────────────────────────────────────────────")
if not NETLIFY_PAT:
    check("Netlify PAT", False, "NETLIFY_PAT not set — add to main-site-deploy/.env")
else:
    try:
        r = requests.get("https://api.netlify.com/api/v1/user",
                         headers={"Authorization": f"Bearer {NETLIFY_PAT}"}, timeout=10)
        if r.status_code == 200:
            email = r.json().get("email", "")
            check("Netlify PAT", True, email)
        else:
            check("Netlify PAT", False,
                  f"HTTP {r.status_code} — PAT likely expired, generate a new one in Netlify dashboard")
    except Exception as e:
        check("Netlify PAT", False, str(e))


# ── 4. ElevenLabs ─────────────────────────────────────────────────────────────
print("\n── ElevenLabs ───────────────────────────────────────────────────────────")
if not EL_API_KEY:
    check("ElevenLabs API key", False, "ELEVENLABS_API_KEY not set")
else:
    try:
        r = requests.get("https://api.elevenlabs.io/v1/user",
                         headers={"xi-api-key": EL_API_KEY}, timeout=10)
        check("ElevenLabs API key", r.status_code == 200,
              f"HTTP {r.status_code}" if r.status_code != 200 else "valid")
    except Exception as e:
        check("ElevenLabs API key", False, str(e))

    if EL_AGENT_ID and EL_API_KEY:
        try:
            r2 = requests.get(f"https://api.elevenlabs.io/v1/convai/agents/{EL_AGENT_ID}",
                              headers={"xi-api-key": EL_API_KEY}, timeout=10)
            if r2.status_code == 200:
                name = r2.json().get("name", EL_AGENT_ID)
                check("ElevenLabs agent ID", True, name)
            else:
                check("ElevenLabs agent ID", False,
                      f"HTTP {r2.status_code} — agent may not exist in this account")
        except Exception as e:
            check("ElevenLabs agent ID", False, str(e))
    else:
        check("ElevenLabs agent ID", False, "ELEVENLABS_AGENT_ID not set")


# ── 5. Render server ──────────────────────────────────────────────────────────
print("\n── Render server ────────────────────────────────────────────────────────")
try:
    r = requests.get(RENDER_URL, timeout=15)
    if r.status_code == 200:
        data = r.json()
        ghl_ok = data.get("ghl") == "ok"
        reason = data.get("reason", "")
        check("Render server reachable", True, RENDER_URL)
        check("Server GHL health", ghl_ok,
              "all dependencies verified" if ghl_ok
              else f"{reason} — check Render startup logs")
    else:
        check("Render server reachable", False, f"HTTP {r.status_code}")
except Exception as e:
    check("Render server reachable", False, str(e))


# ── Summary ───────────────────────────────────────────────────────────────────
passed = sum(1 for r in results if r[0] == "✅")
failed = sum(1 for r in results if r[0] == "❌")
print(f"\n{'='*70}")
print(f"  AUDIT COMPLETE:  {passed} passed  |  {failed} failed")
if failed:
    print("\n  Fix needed:")
    for icon, label, detail in results:
        if icon == "❌":
            print(f"    • {label}: {detail}")
print(f"{'='*70}\n")

sys.exit(1 if failed else 0)
