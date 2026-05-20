#!/usr/bin/env python3
"""
GHL Lead Response — Webhook Server Mode
GHL calls this server the instant a new contact is created.
Deploy behind ngrok or a public server and register the URL in GHL.

Setup:
  pip install flask twilio
  python webhook_server.py

Register in GHL:
  Settings > Integrations > Webhooks > Add Webhook
  URL: http://your-public-ip:5000/new-lead       → Event: Contact Created
  URL: http://your-public-ip:5000/inbound-sms    → Event: Inbound Message (SMS)
  URL: http://your-public-ip:5000/inbound-alert  → Event: Inbound Message (all channels)
"""

import os
import logging
import sys

from twilio.rest import Client as TwilioClient

sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, request, jsonify
from send_lead_response import (
    SKIP_TAG,
    PRICING_TAG,
    BOOKING_TAG,
    FOLLOWUP_TAG,
    get_headers,
    get_contact_full,
    get_custom_field_definitions,
    extract_custom_fields,
    build_campaign_data,
    get_or_create_conversation,
    send_sms,
    send_pricing_sms,
    send_booking_sms,
    send_followup_sms,
    send_talk_sms,
    send_email,
    tag_contact,
    send_owner_notification,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/new-lead", methods=["POST"])
def new_lead():
    """GHL sends a POST here when a new contact is created."""
    data = request.get_json(force=True) or {}
    log.info(f"New-lead webhook received. Keys: {list(data.keys())}")

    contact = data.get("contact") or {}
    contact_id = contact.get("id") or data.get("contact_id")
    first_name = contact.get("firstName", "") or data.get("first_name", "")
    phone = contact.get("phone", "") or data.get("phone", "")
    email = contact.get("email", "") or data.get("email", "")

    if not contact_id:
        log.warning("No contact ID in webhook payload — ignoring.")
        return jsonify({"status": "ignored", "reason": "no contact_id"}), 200

    location_id = os.environ.get("GHL_LOCATION_ID", "")
    headers = get_headers()

    # Always fetch fresh tags from GHL — payload tags are unreliable (may be string or stale)
    full_contact = get_contact_full(headers, contact_id)
    existing_tags = full_contact.get("tags", []) if full_contact else []

    if SKIP_TAG in existing_tags:
        log.info(f"Contact {contact_id} already contacted. Skipping.")
        return jsonify({"status": "skipped"}), 200

    # Fill in any fields the webhook payload left blank
    if full_contact:
        first_name = first_name or full_contact.get("firstName", "")
        phone = phone or full_contact.get("phone", "")
        email = email or full_contact.get("email", "")

    field_defs = get_custom_field_definitions(headers, location_id)
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
        return jsonify({"status": "error", "reason": "could not create conversation"}), 500

    if phone:
        send_sms(headers, contact_id, conversation_id, phone, first_name, campaign_data)

    send_email(headers, contact_id, conversation_id, email, first_name, campaign_data)
    tag_contact(headers, contact_id, existing_tags)

    last_name = full_contact.get("lastName", "") if full_contact else ""
    send_owner_notification(headers, location_id, first_name, last_name, phone, campaign_data)

    return jsonify({"status": "sent"}), 200


@app.route("/inbound-sms", methods=["POST"])
def inbound_sms():
    """
    GHL sends a POST here when a lead replies via SMS.
    Fires back the pricing message based on their lead form data.

    Register in GHL:
      Settings > Integrations > Webhooks > Add Webhook
      Event: Inbound Message  (or Conversation InboundMessage)
      URL: http://your-public-ip:5000/inbound-sms
    """
    data = request.get_json(force=True) or {}
    log.info(f"Inbound SMS webhook received. Keys: {list(data.keys())}")

    # GHL Workflow flat payloads nest direction/body/conversationId inside a 'message' object
    msg = data.get("message") or {}
    if not isinstance(msg, dict):
        msg = {}

    direction = (
        data.get("direction")
        or data.get("messageDirection")
        or msg.get("direction")
        or msg.get("messageDirection")
        or data.get("type", "")
    ).lower()

    contact_id = data.get("contactId") or data.get("contact_id") or msg.get("contactId")
    conversation_id = (
        data.get("conversationId")
        or data.get("conversation_id")
        or msg.get("conversationId")
        or msg.get("conversation_id")
    )

    if direction and "inbound" not in direction:
        log.info(f"Not an inbound message (direction={direction!r}). Skipping.")
        return jsonify({"status": "ignored", "reason": "not inbound"}), 200

    if not contact_id:
        log.warning("Missing contactId. Ignoring.")
        return jsonify({"status": "ignored", "reason": "missing contact_id"}), 200

    headers = get_headers()
    location_id = os.environ.get("GHL_LOCATION_ID", "")

    full_contact = get_contact_full(headers, contact_id)
    if not full_contact:
        log.error(f"Could not fetch contact {contact_id}.")
        return jsonify({"status": "error"}), 500

    # Fall back to get_or_create if conversationId wasn't in the payload
    if not conversation_id:
        conversation_id = get_or_create_conversation(headers, contact_id, location_id)
    if not conversation_id:
        log.error(f"Could not get conversation for {contact_id}.")
        return jsonify({"status": "error"}), 500

    existing_tags = full_contact.get("tags", [])
    first_name = full_contact.get("firstName", "there")
    phone = full_contact.get("phone", "")
    body = (data.get("body") or msg.get("body") or msg.get("text") or "").strip().upper()
    log.info(f"Contact {contact_id} | tags={existing_tags} | body={body!r}")

    # Stage 3: lead already got pricing — check if they replied BOOK or TALK
    if PRICING_TAG in existing_tags:
        if BOOKING_TAG in existing_tags:
            log.info(f"Booking link already sent to {contact_id}. Skipping.")
            return jsonify({"status": "skipped"}), 200

        if "BOOK" in body:
            # Fetch campaign data so the booking URL includes the correct service label
            location_id = os.environ.get("GHL_LOCATION_ID", "")
            field_defs = get_custom_field_definitions(headers, location_id)
            custom_fields = extract_custom_fields(full_contact, field_defs)
            campaign_data = build_campaign_data(custom_fields)
            success = send_booking_sms(headers, contact_id, conversation_id, phone, first_name, full_contact, campaign_data)
            if success:
                tag_contact(headers, contact_id, existing_tags, extra_tags=[BOOKING_TAG])
                return jsonify({"status": "booking_link_sent"}), 200
            return jsonify({"status": "error"}), 500

        if "TALK" in body or "REP" in body or "CALL" in body:
            success = send_talk_sms(headers, contact_id, conversation_id, phone, first_name)
            if success:
                tag_contact(headers, contact_id, existing_tags, extra_tags=[BOOKING_TAG])
                return jsonify({"status": "talk_link_sent"}), 200
            return jsonify({"status": "error"}), 500

        log.info(f"Pricing sent but reply '{body[:30]}' is not BOOK/TALK — ignoring.")
        return jsonify({"status": "skipped", "reason": "awaiting book/talk reply"}), 200

    # Stage 2: lead confirmed details — send pricing
    if SKIP_TAG not in existing_tags:
        log.info(f"Initial message not yet sent for {contact_id}. Skipping pricing.")
        return jsonify({"status": "skipped"}), 200

    field_defs = get_custom_field_definitions(headers, location_id)
    custom_fields = extract_custom_fields(full_contact, field_defs)
    campaign_data = build_campaign_data(custom_fields)

    success = send_pricing_sms(
        headers, contact_id, conversation_id, phone, first_name, campaign_data
    )

    if success:
        tag_contact(headers, contact_id, existing_tags, extra_tags=[PRICING_TAG])
        return jsonify({"status": "pricing_sent"}), 200
    else:
        return jsonify({"status": "error"}), 500


@app.route("/follow-up", methods=["POST"])
def follow_up():
    """
    GHL sends a POST here ~24h after Stage 1 if the lead hasn't replied YES.
    Sends one follow-up SMS reminding them to confirm their quote.

    Register in GHL:
      Automation → Workflow → Trigger: Contact Tag Added (initial-response-sent)
      Wait 24h → If/Else: pricing-sent tag does NOT exist → Webhook POST here
    """
    data = request.get_json(force=True) or {}
    contact_id = (data.get("contact") or {}).get("id") or data.get("contact_id")

    if not contact_id:
        log.warning("No contact ID in follow-up payload — ignoring.")
        return jsonify({"status": "ignored", "reason": "no contact_id"}), 200

    headers = get_headers()
    location_id = os.environ.get("GHL_LOCATION_ID", "")

    full_contact = get_contact_full(headers, contact_id)
    if not full_contact:
        return jsonify({"status": "error", "reason": "could not fetch contact"}), 500

    existing_tags = full_contact.get("tags", [])

    if PRICING_TAG in existing_tags:
        log.info(f"Contact {contact_id} already responded — skipping follow-up.")
        return jsonify({"status": "skipped", "reason": "already responded"}), 200

    if FOLLOWUP_TAG in existing_tags:
        log.info(f"Follow-up already sent to {contact_id} — skipping.")
        return jsonify({"status": "skipped", "reason": "follow-up already sent"}), 200

    first_name = full_contact.get("firstName", "there")
    phone = full_contact.get("phone", "")

    field_defs = get_custom_field_definitions(headers, location_id)
    custom_fields = extract_custom_fields(full_contact, field_defs)
    campaign_data = build_campaign_data(custom_fields)

    conversation_id = get_or_create_conversation(headers, contact_id, location_id)
    if not conversation_id:
        return jsonify({"status": "error", "reason": "could not get conversation"}), 500

    success = send_followup_sms(headers, contact_id, conversation_id, phone, first_name, campaign_data)
    if success:
        tag_contact(headers, contact_id, existing_tags, extra_tags=[FOLLOWUP_TAG])
        return jsonify({"status": "follow_up_sent"}), 200
    return jsonify({"status": "error"}), 500


@app.route("/inbound-alert", methods=["POST"])
def inbound_alert():
    """
    GHL sends a POST here for every inbound message (SMS, Facebook, Instagram, etc.).
    Fires an SMS alert to the owner's phone via Twilio so they never miss a reply.

    Register in GHL:
      Settings > Integrations > Webhooks > Add Webhook
      Event: Inbound Message (all channels)
      URL: http://your-public-ip:5000/inbound-alert

    Required env vars:
      TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER
      GHL_OWNER_PHONE  (defaults to +17252968281)
    """
    data = request.get_json(force=True) or {}

    direction = (
        data.get("direction")
        or data.get("messageDirection")
        or ""
    ).lower()

    if "inbound" not in direction:
        return jsonify({"status": "ignored", "reason": "not inbound"}), 200

    body = (data.get("body") or data.get("message") or "").strip()
    if not body:
        return jsonify({"status": "ignored", "reason": "empty body"}), 200

    # Extract contact details from payload
    contact    = data.get("contact", {}) or {}
    first_name = contact.get("firstName") or data.get("firstName") or "Someone"
    last_name  = contact.get("lastName")  or data.get("lastName")  or ""
    phone      = contact.get("phone")     or data.get("phone")     or "unknown number"
    channel    = (data.get("messageType") or data.get("channel") or "message").upper()

    name = f"{first_name} {last_name}".strip()
    snippet = body[:120] + ("…" if len(body) > 120 else "")

    alert_text = (
        f"New {channel} from {name} ({phone}):\n"
        f'"{snippet}"\n'
        f"Reply in GHL or call them back."
    )

    owner_phone   = os.environ.get("GHL_OWNER_PHONE", "+17252968281")
    twilio_sid    = os.environ.get("TWILIO_ACCOUNT_SID", "")
    twilio_token  = os.environ.get("TWILIO_AUTH_TOKEN", "")
    twilio_from   = os.environ.get("TWILIO_PHONE_NUMBER", "")

    if not all([twilio_sid, twilio_token, twilio_from]):
        log.error("Twilio credentials missing — cannot send owner alert.")
        return jsonify({"status": "error", "reason": "missing twilio credentials"}), 500

    try:
        client = TwilioClient(twilio_sid, twilio_token)
        client.messages.create(to=owner_phone, from_=twilio_from, body=alert_text)
        log.info(f"Owner alert sent to {owner_phone} — {name} via {channel}")
        return jsonify({"status": "alert_sent"}), 200
    except Exception as e:
        log.error(f"Failed to send owner alert: {e}")
        return jsonify({"status": "error", "reason": str(e)}), 500


@app.route("/pricing", methods=["GET"])
def pricing():
    """Returns the current pricing data as JSON — used by the instant quote website."""
    import json, os
    pricing_file = os.path.join(os.path.dirname(__file__), "pricing.json")
    with open(pricing_file) as f:
        data = json.load(f)
    response = jsonify(data)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response, 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"Webhook server listening on port {port}")
    log.info(f"Register in GHL:")
    log.info(f"  /new-lead       → Event: Contact Created")
    log.info(f"  /inbound-sms    → Event: Inbound Message (SMS, for pricing reply)")
    log.info(f"  /inbound-alert  → Event: Inbound Message (all channels, owner alert)")
    app.run(host="0.0.0.0", port=port)
