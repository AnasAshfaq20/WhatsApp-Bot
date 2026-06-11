from flask import Blueprint, request

from .. import config
from ..db import get_owner_by_phone_id
from ..services import bot, whatsapp

webhook_bp = Blueprint("webhook", __name__)

# Deduplication — Meta sometimes retries webhooks
processed_message_ids = set()


@webhook_bp.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == config.VERIFY_TOKEN:
        print("Webhook verified successfully")
        return challenge or "", 200
    return "Verification failed", 403


@webhook_bp.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json(silent=True) or {}
    try:
        entry   = data.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value   = changes.get("value", {})

        # Skip delivery/read receipts
        if value.get("statuses"):
            return "EVENT_RECEIVED", 200

        messages = value.get("messages", [])
        if not messages:
            return "EVENT_RECEIVED", 200

        # Route to the right owner by the receiving phone number ID
        phone_id = value.get("metadata", {}).get("phone_number_id", "")
        owner = get_owner_by_phone_id(phone_id)
        if not owner:
            print(f"No active owner for phone_number_id={phone_id} — message dropped")
            return "EVENT_RECEIVED", 200

        msg        = messages[0]
        sender     = msg.get("from")
        m_type     = msg.get("type", "")
        message_id = msg.get("id", "")

        if message_id and message_id in processed_message_ids:
            print(f"Duplicate ignored: {message_id}")
            return "EVENT_RECEIVED", 200
        if message_id:
            processed_message_ids.add(message_id)
            if len(processed_message_ids) > 1000:
                processed_message_ids.pop()

        if m_type == "audio":
            try:
                media_id     = msg["audio"]["id"]
                incoming_msg = whatsapp.transcribe_audio(owner, media_id)
                print(f"[{owner['restaurant_name']}] {sender} | Voice transcribed: {incoming_msg}")
            except Exception as e:
                print(f"Transcription failed: {e}")
                whatsapp.send_text(owner, sender,
                    "Sorry, I could not understand your voice message. Please try again or type your order.")
                return "EVENT_RECEIVED", 200
        elif m_type == "text":
            incoming_msg = msg["text"]["body"].strip()
            print(f"[{owner['restaurant_name']}] {sender} | Msg: {incoming_msg}")
        else:
            whatsapp.send_text(owner, sender, "Sorry, I can only handle text and voice messages.")
            return "EVENT_RECEIVED", 200

        reply = bot.chat(owner, sender, incoming_msg)
        if reply:
            print(f"Reply: {reply}")
            whatsapp.send_text(owner, sender, reply)

    except Exception as e:
        print("Error processing webhook:", e)

    return "EVENT_RECEIVED", 200
