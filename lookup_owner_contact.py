#!/usr/bin/env python3
"""
One-shot script: finds the blues owners contact in GHL and prints the
exact env var values to paste into Render/Railway.

Usage:
    export GHL_API_KEY="..."
    export GHL_LOCATION_ID="..."
    python lookup_owner_contact.py
"""

import os
import sys
import requests

BASE_URL = "https://services.leadconnectorhq.com"
OWNER_PHONE = os.environ.get("GHL_OWNER_PHONE", "+17252968281")


def require_env(name):
    val = os.environ.get(name, "")
    if not val:
        sys.exit(f"ERROR: {name} env var is not set.")
    return val


def main():
    api_key = require_env("GHL_API_KEY")
    location_id = require_env("GHL_LOCATION_ID")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }

    print(f"Looking up contact for phone: {OWNER_PHONE}")
    resp = requests.get(
        f"{BASE_URL}/contacts/",
        headers=headers,
        params={"locationId": location_id, "query": OWNER_PHONE},
    )
    if resp.status_code != 200:
        sys.exit(f"ERROR: contacts lookup failed — {resp.status_code} {resp.text}")

    contacts = resp.json().get("contacts", [])
    if not contacts:
        sys.exit(f"ERROR: No GHL contact found for phone {OWNER_PHONE}. Check GHL_OWNER_PHONE.")

    contact = contacts[0]
    contact_id = contact["id"]
    name = f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip()
    print(f"Found contact: {name} (ID: {contact_id})")

    # Get or create the conversation
    resp2 = requests.get(
        f"{BASE_URL}/conversations/search",
        headers=headers,
        params={"locationId": location_id, "contactId": contact_id},
    )
    conv_id = None
    if resp2.status_code == 200:
        convs = resp2.json().get("conversations", [])
        if convs:
            conv_id = convs[0]["id"]

    if not conv_id:
        resp3 = requests.post(
            f"{BASE_URL}/conversations/",
            headers=headers,
            json={"locationId": location_id, "contactId": contact_id},
        )
        if resp3.status_code in (200, 201):
            conv_id = resp3.json().get("conversation", {}).get("id")

    if not conv_id:
        sys.exit("ERROR: Could not find or create a conversation for this contact.")

    print(f"Conversation ID: {conv_id}")
    print()
    print("=" * 60)
    print("Paste these into your Render or Railway env vars:")
    print("=" * 60)
    print(f"GHL_OWNER_PHONE={OWNER_PHONE}")
    print(f"GHL_OWNER_CONTACT_ID={contact_id}")
    print(f"GHL_OWNER_CONV_ID={conv_id}")
    print("=" * 60)
    print()
    print("Also REMOVE any old GHL_OWNER_CONTACT_ID / GHL_OWNER_CONV_ID values")
    print("that were pointing to Leo's contact before setting the new ones.")


if __name__ == "__main__":
    main()
