#!/usr/bin/env python3
"""
GHL Lead Response — Polling Mode
Queries GHL for uncontacted leads created in the last 30 minutes and
sends each one a personalized SMS + email through GHL's messaging API.
"""

import os
import re
import sys
import time
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

BASE_URL = "https://services.leadconnectorhq.com"
SKIP_TAG = "initial-response-sent"
PRICING_TAG = "pricing-sent"
BOOKING_TAG = "booking-link-sent"
FOLLOWUP_TAG = "follow-up-sent"

BOOKING_URL = "https://api.leadconnectorhq.com/widget/booking/WcMuX2qHzZeszRG4AzTM"
CONSULT_URL = "https://api.leadconnectorhq.com/widget/bookings/blues-phone-consultation"

# Maps campaign type → service label used in the booking page URL
SERVICE_LABELS = {
    "windows": "Window Cleaning",
    "solar":   "Solar Panel Cleaning",
}
LOOKBACK_MINUTES = 30

# Load pricing from pricing.json (single source of truth for all pricing)
_PRICING_FILE = os.path.join(os.path.dirname(__file__), "pricing.json")
with open(_PRICING_FILE) as _f:
    _PRICING = json.load(_f)

WINDOW_RATES = {
    "exterior_no_screens":      _PRICING["window_cleaning"]["exterior_no_screens"],
    "exterior_with_screens":    _PRICING["window_cleaning"]["exterior_with_screens"],
    "interior_add_no_screens":  _PRICING["window_cleaning"]["interior_add_no_screens"],
    "interior_add_with_screens":_PRICING["window_cleaning"]["interior_add_with_screens"],
}
MAX_WINDOW_COUNT = int(_PRICING["window_cleaning"]["max_window_count"])

SOLAR_RATES = {
    "single": _PRICING["solar_panels"]["single_story_per_panel"],
    "double": _PRICING["solar_panels"]["double_story_per_panel"],
}


def get_headers():
    api_key = os.environ.get("GHL_API_KEY")
    if not api_key:
        log.error("GHL_API_KEY environment variable is not set.")
        sys.exit(1)
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Version": "2021-07-28",
    }


def get_location_id():
    loc_id = os.environ.get("GHL_LOCATION_ID")
    if not loc_id:
        log.error("GHL_LOCATION_ID environment variable is not set.")
        sys.exit(1)
    return loc_id


def load_template(filename):
    template_path = os.path.join(
        os.path.dirname(__file__), "templates", filename
    )
    with open(template_path, "r") as f:
        return f.read()


def render_template(template_name, replacements):
    body = load_template(template_name)
    for key, value in replacements.items():
        body = body.replace("{{" + key + "}}", value or "")
    return body


def get_recent_contacts(headers, location_id):
    since = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
    since_ts = int(since.timestamp() * 1000)

    url = f"{BASE_URL}/contacts/"
    params = {
        "locationId": location_id,
        "startAfter": since_ts,
        "limit": 100,
    }

    resp = requests.get(url, headers=headers, params=params)
    if resp.status_code != 200:
        log.error(f"Failed to fetch contacts: {resp.status_code} {resp.text}")
        return []

    contacts = resp.json().get("contacts", [])
    log.info(f"Found {len(contacts)} contacts created in last {LOOKBACK_MINUTES} minutes.")
    return contacts


def get_contact_full(headers, contact_id):
    """Fetch the full contact record including custom fields."""
    resp = requests.get(f"{BASE_URL}/contacts/{contact_id}", headers=headers)
    if resp.status_code != 200:
        log.warning(f"Could not fetch full contact {contact_id}: {resp.status_code}")
        return {}
    return resp.json().get("contact", {})


def get_custom_field_definitions(headers, location_id):
    """Return {field_name_lower: field_id} for all location custom fields."""
    resp = requests.get(f"{BASE_URL}/locations/{location_id}/customFields", headers=headers)
    if resp.status_code != 200:
        log.warning(f"Could not fetch custom field definitions: {resp.status_code}")
        return {}
    fields = resp.json().get("customFields", [])
    return {f["name"].lower(): f["id"] for f in fields}


def extract_custom_fields(contact, field_id_map):
    """
    Pull custom field values from a contact record.
    Returns {field_name_lower: value} using field_id_map for name lookup.
    """
    id_to_name = {v: k for k, v in field_id_map.items()}
    result = {}
    for cf in contact.get("customFields", []):
        field_id = cf.get("id", "")
        value = cf.get("value", "")
        if field_id in id_to_name:
            result[id_to_name[field_id]] = value
    return result


def detect_campaign(custom_fields):
    """Return 'windows', 'solar', or 'unknown' based on Service Interest field."""
    service = (custom_fields.get("service interest") or "").lower()
    if "solar" in service or "panel" in service:
        return "solar"
    if "window" in service:
        return "windows"
    # Fallback: check which count field is populated
    if custom_fields.get("number of solar panels"):
        return "solar"
    if custom_fields.get("number of windows"):
        return "windows"
    return "unknown"


def parse_window_count(window_count_text):
    """Extract min and max from free-text window count (e.g. '5-15' → (5, 15), '10' → (10, 10))."""
    numbers = re.findall(r'\d+', window_count_text or "")
    if not numbers:
        return None, None
    ints = [int(n) for n in numbers]
    return min(ints), max(ints)


def _fmt(amount):
    """Format a dollar amount — no decimals if whole, two decimals otherwise."""
    return f"${amount:.0f}" if amount == int(amount) else f"${amount:.2f}"


def _price_range(low, high):
    """Return '$X' if low==high, else '$X–$Y'."""
    return _fmt(low) if low == high else f"{_fmt(low)}–{_fmt(high)}"


def get_window_price(window_count_text):
    """
    Return a multi-line pricing breakdown for all four service options.
    Shows a price range when the lead submitted a window count range (e.g. '5–15').
    """
    min_c, max_c = parse_window_count(window_count_text)
    if min_c is None:
        return "Reply with your window count and I'll get you a price right away!"
    if min_c > MAX_WINDOW_COUNT:
        return "Reply for a custom quote — our team will get you an exact price!"

    max_c = min(max_c, MAX_WINDOW_COUNT)
    r = WINDOW_RATES

    return (
        f"Exterior only (no screen cleaning): {_price_range(min_c * r['exterior_no_screens'], max_c * r['exterior_no_screens'])}\n"
        f"Exterior only (with screen cleaning): {_price_range(min_c * r['exterior_with_screens'], max_c * r['exterior_with_screens'])}\n"
        f"Interior + Exterior (no screen cleaning): {_price_range(min_c * (r['exterior_no_screens'] + r['interior_add_no_screens']), max_c * (r['exterior_no_screens'] + r['interior_add_no_screens']))}\n"
        f"Interior + Exterior (with screen cleaning): {_price_range(min_c * (r['exterior_with_screens'] + r['interior_add_with_screens']), max_c * (r['exterior_with_screens'] + r['interior_add_with_screens']))}"
    )


def parse_panel_range(panel_range_text):
    """Parse '1–10', '20–30', or '26+' into (min, max). '+' suffix means up to 100 panels."""
    text = panel_range_text or ""
    has_plus = "+" in text
    numbers = re.findall(r'\d+', text)
    if len(numbers) >= 2:
        return int(numbers[0]), int(numbers[1])
    if len(numbers) == 1:
        n = int(numbers[0])
        return n, 100 if has_plus else n
    return None, None


def get_solar_price(home_stories, panel_range):
    story_key = "double" if "double" in (home_stories or "").lower() else "single"
    rate = SOLAR_RATES[story_key]
    min_p, max_p = parse_panel_range(panel_range)
    if min_p is None:
        return "Reply with your panel count and I'll get you a price right away!"
    low, high = min_p * rate, max_p * rate
    return _price_range(low, high)


def get_appointment_price_label(campaign_data):
    """Return a short price string for a calendar event title (e.g. '$130–$390'), or None."""
    campaign_type = campaign_data.get("type", "")
    if campaign_type == "windows":
        min_c, max_c = parse_window_count(campaign_data.get("window_count", ""))
        if min_c is None:
            return None
        rate = WINDOW_RATES["exterior_no_screens"]
        return _price_range(min_c * rate, min(max_c, MAX_WINDOW_COUNT) * rate)
    if campaign_type == "solar":
        story_key = "double" if "double" in (campaign_data.get("home_stories") or "").lower() else "single"
        rate = SOLAR_RATES[story_key]
        min_p, max_p = parse_panel_range(campaign_data.get("solar_count", ""))
        if min_p is None:
            return None
        return _price_range(min_p * rate, max_p * rate)
    return None


def has_been_contacted(contact):
    return SKIP_TAG in contact.get("tags", [])


def tag_contact(headers, contact_id, existing_tags, extra_tags=None):
    new_tags = list(set(existing_tags + [SKIP_TAG] + (extra_tags or [])))
    url = f"{BASE_URL}/contacts/{contact_id}"
    resp = requests.put(url, headers=headers, json={"tags": new_tags})
    if resp.status_code not in (200, 201):
        log.warning(f"Failed to tag contact {contact_id}: {resp.status_code} {resp.text}")
    else:
        log.info(f"Tagged contact {contact_id}: {new_tags}")


def get_or_create_conversation(headers, contact_id, location_id):
    url = f"{BASE_URL}/conversations/search"
    params = {"locationId": location_id, "contactId": contact_id}
    resp = requests.get(url, headers=headers, params=params)

    if resp.status_code == 200:
        convs = resp.json().get("conversations", [])
        if convs:
            return convs[0]["id"]

    create_resp = requests.post(
        f"{BASE_URL}/conversations/",
        headers=headers,
        json={"locationId": location_id, "contactId": contact_id},
    )
    if create_resp.status_code in (200, 201):
        return create_resp.json().get("conversation", {}).get("id")

    log.error(f"Could not create conversation for {contact_id}: {create_resp.text}")
    return None


def _clean_count(text):
    """Strip word suffixes GHL appends to dropdown values (e.g. '16-25 Panels' → '16-25')."""
    cleaned = re.sub(r'\s*\b(panels?|windows?)\b\s*', '', (text or ""), flags=re.IGNORECASE).strip(" -–")
    return cleaned if cleaned else (text or "").strip()


def build_campaign_data(custom_fields):
    """Package campaign-relevant custom field values for template rendering."""
    campaign_type = detect_campaign(custom_fields)
    home_stories = (custom_fields.get("home stories") or "").strip().lower()
    window_count = _clean_count(custom_fields.get("number of windows") or "")
    solar_count = _clean_count(custom_fields.get("number of solar panels") or "")
    return {
        "type": campaign_type,
        "home_stories": home_stories,
        "window_count": window_count,
        "solar_count": solar_count,
    }


def send_sms(headers, contact_id, conversation_id, phone, first_name, campaign_data=None):
    campaign_data = campaign_data or {}
    campaign = campaign_data.get("type", "unknown")

    if campaign == "windows":
        template_name = "sms_initial_windows.txt"
    elif campaign == "solar":
        template_name = "sms_initial_solar.txt"
    else:
        template_name = "sms_initial.txt"

    message_body = render_template(template_name, {
        "first_name":   first_name or "there",
        "home_stories": campaign_data.get("home_stories") or "single or double story",
        "window_count": campaign_data.get("window_count") or "your",
        "solar_count":  campaign_data.get("solar_count") or "your",
    })

    payload = {
        "type": "SMS",
        "conversationId": conversation_id,
        "contactId": contact_id,
        "message": message_body,
    }
    resp = requests.post(f"{BASE_URL}/conversations/messages", headers=headers, json=payload)
    if resp.status_code in (200, 201):
        log.info(f"SMS sent to {phone} (contact {contact_id})")
        return True
    else:
        log.error(f"SMS failed for {contact_id}: {resp.status_code} {resp.text}")
        return False


def send_pricing_sms(headers, contact_id, conversation_id, phone, first_name, campaign_data):
    """Send the pricing reply SMS after the lead confirms their details."""
    campaign = campaign_data.get("type", "unknown")
    home_stories = campaign_data.get("home_stories", "")
    window_count = campaign_data.get("window_count", "")
    solar_count = campaign_data.get("solar_count", "")

    if campaign == "windows":
        price_range = get_window_price(window_count)
        message_body = render_template("sms_pricing_windows.txt", {
            "first_name":   first_name or "there",
            "home_stories": home_stories or "your",
            "window_count": window_count or "your",
            "price_range":  price_range,
        })
    elif campaign == "solar":
        price_range = get_solar_price(home_stories, solar_count)
        message_body = render_template("sms_pricing_solar.txt", {
            "first_name":   first_name or "there",
            "home_stories": home_stories or "your",
            "solar_count":  solar_count or "your",
            "price_range":  price_range,
        })
    else:
        log.warning(f"Unknown campaign for {contact_id} — skipping pricing SMS.")
        return False

    payload = {
        "type": "SMS",
        "conversationId": conversation_id,
        "contactId": contact_id,
        "message": message_body,
    }
    resp = requests.post(f"{BASE_URL}/conversations/messages", headers=headers, json=payload)
    if resp.status_code in (200, 201):
        log.info(f"Pricing SMS sent to {phone} (contact {contact_id})")
        return True
    else:
        log.error(f"Pricing SMS failed for {contact_id}: {resp.status_code} {resp.text}")
        return False


def build_booking_url(contact, campaign_data=None):
    """Build a GHL calendar booking URL tied to the existing contact to avoid duplicate-contact errors."""
    contact_id = contact.get("id") or contact.get("contactId") if contact else None
    if contact_id:
        return f"{BOOKING_URL}?contactId={contact_id}"
    # Fallback when no contact ID is available: pre-fill fields instead
    params = {}
    params["firstName"] = contact.get("firstName", "") if contact else ""
    params["lastName"]  = contact.get("lastName", "")  if contact else ""
    params["phone"]     = contact.get("phone", "")     if contact else ""
    params["email"]     = contact.get("email", "")     if contact else ""
    return f"{BOOKING_URL}?{urlencode(params)}"


def send_booking_sms(headers, contact_id, conversation_id, phone, first_name, contact=None, campaign_data=None):
    """Send a pre-filled booking link so the lead only needs to pick a date and time."""
    booking_url = build_booking_url(contact, campaign_data) if contact else BOOKING_URL
    message_body = render_template("sms_booking_link.txt", {
        "first_name": first_name or "there",
        "booking_url": booking_url,
    })
    payload = {
        "type": "SMS",
        "conversationId": conversation_id,
        "contactId": contact_id,
        "message": message_body,
    }
    resp = requests.post(f"{BASE_URL}/conversations/messages", headers=headers, json=payload)
    if resp.status_code in (200, 201):
        log.info(f"Booking link SMS sent to {phone} (contact {contact_id})")
        return True
    log.error(f"Booking SMS failed for {contact_id}: {resp.status_code} {resp.text}")
    return False


def get_owner_conversation(headers, location_id):
    """Return (contact_id, conversation_id) for the owner notification contact."""
    owner_phone = os.environ.get("GHL_OWNER_PHONE", "+17252968281")
    resp = requests.get(f"{BASE_URL}/contacts/", headers=headers,
                        params={"locationId": location_id, "query": owner_phone})
    contacts = resp.json().get("contacts", []) if resp.status_code == 200 else []
    if not contacts:
        log.warning("Owner contact not found in GHL — skipping owner notification.")
        return None, None
    contact_id = contacts[0]["id"]
    conv_id = get_or_create_conversation(headers, contact_id, location_id)
    return contact_id, conv_id


def send_owner_notification(headers, location_id, first_name, last_name, phone, campaign_data):
    """Send owner an SMS via GHL when a new lead arrives."""
    campaign = campaign_data.get("type", "unknown")
    service = "Solar Panel Cleaning" if campaign == "solar" else "Window Cleaning"
    count = campaign_data.get("solar_count") or campaign_data.get("window_count") or "?"
    stories = campaign_data.get("home_stories", "").title() or "?"

    message = (
        f"\U0001f514 New lead: {first_name} {last_name}\n"
        f"Phone: {phone}\n"
        f"Service: {service}\n"
        f"Details: {count} | {stories}\n"
        f"Auto-SMS sent ✓"
    )

    owner_contact_id, owner_conv_id = get_owner_conversation(headers, location_id)
    if not owner_contact_id or not owner_conv_id:
        return

    resp = requests.post(
        f"{BASE_URL}/conversations/messages",
        headers=headers,
        json={"type": "SMS", "conversationId": owner_conv_id,
              "contactId": owner_contact_id, "message": message},
    )
    if resp.status_code in (200, 201):
        log.info(f"Owner notified of new lead: {first_name} {last_name}")
    else:
        log.warning(f"Owner notification failed: {resp.status_code} {resp.text}")


def send_followup_sms(headers, contact_id, conversation_id, phone, first_name, campaign_data):
    """Send a follow-up SMS to leads who haven't replied to Stage 1."""
    campaign = campaign_data.get("type", "unknown")
    service_type = "solar panel cleaning" if campaign == "solar" else "window cleaning"
    message_body = render_template("sms_followup.txt", {
        "first_name": first_name or "there",
        "service_type": service_type,
    })
    payload = {
        "type": "SMS",
        "conversationId": conversation_id,
        "contactId": contact_id,
        "message": message_body,
    }
    resp = requests.post(f"{BASE_URL}/conversations/messages", headers=headers, json=payload)
    if resp.status_code in (200, 201):
        log.info(f"Follow-up SMS sent to {phone} (contact {contact_id})")
        return True
    log.error(f"Follow-up SMS failed for {contact_id}: {resp.status_code} {resp.text}")
    return False


def send_talk_sms(headers, contact_id, conversation_id, phone, first_name):
    """Send the phone consultation link after lead replies TALK."""
    message_body = render_template("sms_talk_link.txt", {"first_name": first_name or "there"})
    payload = {
        "type": "SMS",
        "conversationId": conversation_id,
        "contactId": contact_id,
        "message": message_body,
    }
    resp = requests.post(f"{BASE_URL}/conversations/messages", headers=headers, json=payload)
    if resp.status_code in (200, 201):
        log.info(f"Talk link SMS sent to {phone} (contact {contact_id})")
        return True
    log.error(f"Talk SMS failed for {contact_id}: {resp.status_code} {resp.text}")
    return False


def send_email(headers, contact_id, conversation_id, email, first_name, campaign_data=None):
    if not email:
        log.warning(f"No email address for contact {contact_id}, skipping email.")
        return False

    campaign_data = campaign_data or {}
    campaign = campaign_data.get("type", "unknown")

    if campaign == "windows":
        service_line = f"window cleaning ({campaign_data.get('window_count', '')} windows, {campaign_data.get('home_stories', '')} home)"
    elif campaign == "solar":
        service_line = f"solar panel cleaning ({campaign_data.get('solar_count', '')} panels)"
    else:
        service_line = "window or solar panel cleaning"

    html_template = load_template("email_initial.html")
    html_body = (html_template
                 .replace("{{first_name}}", first_name or "there")
                 .replace("{{service_line}}", service_line))

    payload = {
        "type": "Email",
        "conversationId": conversation_id,
        "contactId": contact_id,
        "subject": "We got your request — check your texts!",
        "html": html_body,
        "emailFrom": os.environ.get("GHL_FROM_EMAIL", ""),
        "emailReplyTo": os.environ.get("GHL_FROM_EMAIL", ""),
        "emailTo": email,
    }
    resp = requests.post(f"{BASE_URL}/conversations/messages", headers=headers, json=payload)
    if resp.status_code in (200, 201):
        log.info(f"Email sent to {email} (contact {contact_id})")
        return True
    else:
        log.error(f"Email failed for {contact_id}: {resp.status_code} {resp.text}")
        return False


def main():
    headers = get_headers()
    location_id = get_location_id()

    field_defs = get_custom_field_definitions(headers, location_id)
    time.sleep(0.15)

    contacts = get_recent_contacts(headers, location_id)
    if not contacts:
        log.info("No new contacts to process. Exiting.")
        return

    sent_count = 0
    skipped_count = 0

    for contact in contacts:
        contact_id = contact.get("id")
        first_name = contact.get("firstName", "")
        phone = contact.get("phone", "")
        email = contact.get("email", "")
        existing_tags = contact.get("tags", [])

        if has_been_contacted(contact):
            log.info(f"Skipping {contact_id} — already tagged as contacted.")
            skipped_count += 1
            continue

        log.info(f"Processing new lead: {first_name} ({phone} / {email})")

        full_contact = get_contact_full(headers, contact_id)
        time.sleep(0.15)

        custom_fields = extract_custom_fields(full_contact or contact, field_defs)
        campaign_data = build_campaign_data(custom_fields)
        log.info(
            f"Campaign: {campaign_data['type']} | "
            f"Stories: {campaign_data['home_stories']} | "
            f"Windows: {campaign_data['window_count']} | "
            f"Solar: {campaign_data['solar_count']}"
        )

        conversation_id = get_or_create_conversation(headers, contact_id, location_id)
        if not conversation_id:
            log.error(f"Skipping {contact_id} — could not get/create conversation.")
            continue

        sms_ok = False
        email_ok = False

        if phone:
            sms_ok = send_sms(headers, contact_id, conversation_id, phone, first_name, campaign_data)
            time.sleep(0.15)

        email_ok = send_email(headers, contact_id, conversation_id, email, first_name, campaign_data)
        time.sleep(0.15)

        if sms_ok or email_ok:
            tag_contact(headers, contact_id, existing_tags)
            sent_count += 1

    log.info(f"Done. Sent: {sent_count}, Skipped (already contacted): {skipped_count}")


if __name__ == "__main__":
    main()
