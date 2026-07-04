from fastapi import APIRouter, Request, Body
from fastapi.responses import PlainTextResponse

from .. import config
from ..db import get_owner_by_phone_id, get_owner_by_fb_page, get_owner_by_ig_account
from ..services import bot, whatsapp, channels

router = APIRouter()

# Deduplication — Meta sometimes retries webhooks
processed_message_ids = set()


def _seen(message_id):
    if not message_id:
        return False
    if message_id in processed_message_ids:
        return True
    processed_message_ids.add(message_id)
    if len(processed_message_ids) > 1000:
        processed_message_ids.pop()
    return False


@router.get("/webhook")
def verify_webhook(request: Request):
    mode      = request.query_params.get("hub.mode")
    token     = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == config.VERIFY_TOKEN:
        print("Webhook verified successfully")
        return PlainTextResponse(challenge or "")
    return PlainTextResponse("Verification failed", status_code=403)


@router.post("/webhook")
def receive_webhook(data: dict = Body(default={})):
    try:
        obj = data.get("object", "")
        # WhatsApp payloads carry entry[].changes; be lenient if object is missing
        if obj == "whatsapp_business_account" or (
                not obj and data.get("entry", [{}])[0].get("changes")):
            _handle_whatsapp(data)
        elif obj == "page":
            _handle_page(data, channels.FACEBOOK)
        elif obj == "instagram":
            _handle_page(data, channels.INSTAGRAM)
        else:
            print(f"Unhandled webhook object type: {obj!r}")
    except Exception as e:
        print("Error processing webhook:", e)

    return PlainTextResponse("EVENT_RECEIVED")


# ── WhatsApp Cloud API ──
def _handle_whatsapp(data):
    entry   = data.get("entry", [])[0]
    changes = entry.get("changes", [])[0]
    value   = changes.get("value", {})

    # Skip delivery/read receipts
    if value.get("statuses"):
        return

    messages = value.get("messages", [])
    if not messages:
        return

    # Route to the right owner by the receiving phone number ID
    phone_id = value.get("metadata", {}).get("phone_number_id", "")
    owner = get_owner_by_phone_id(phone_id)
    if not owner:
        print(f"No active owner for phone_number_id={phone_id} - message dropped")
        return

    msg    = messages[0]
    sender = msg.get("from")
    m_type = msg.get("type", "")

    if _seen(msg.get("id", "")):
        print(f"Duplicate ignored: {msg.get('id')}")
        return

    if m_type == "audio":
        try:
            media_id     = msg["audio"]["id"]
            incoming_msg = whatsapp.transcribe_audio(owner, media_id)
            print(f"[{owner['business_name']}] WA {sender} | Voice transcribed: {incoming_msg}")
        except Exception as e:
            print(f"Transcription failed: {e}")
            whatsapp.send_text(owner, sender,
                "Sorry, I could not understand your voice message. Please try again or type your booking request.")
            return
    elif m_type == "text":
        incoming_msg = msg["text"]["body"].strip()
        print(f"[{owner['business_name']}] WA {sender} | Msg: {incoming_msg}")
    else:
        whatsapp.send_text(owner, sender, "Sorry, I can only handle text and voice messages.")
        return

    reply = bot.chat(owner, sender, incoming_msg, channel=channels.WHATSAPP)
    if reply:
        print(f"Reply: {reply}")
        whatsapp.send_text(owner, sender, reply)


# ── Facebook Messenger + Instagram DM (same event shape) ──
def _audio_attachment_url(message):
    """CDN URL of the first audio attachment on a Messenger/Instagram message."""
    for att in message.get("attachments") or []:
        if att.get("type") == "audio":
            return (att.get("payload") or {}).get("url")
    return None


def _handle_page(data, channel):
    for entry in data.get("entry", []):
        account_id = str(entry.get("id", ""))
        if channel == channels.FACEBOOK:
            owner = get_owner_by_fb_page(account_id)
        else:
            owner = get_owner_by_ig_account(account_id)
        if not owner:
            print(f"No active owner for {channel} account {account_id} - message dropped")
            continue

        for event in entry.get("messaging", []):
            message = event.get("message")
            if not message or message.get("is_echo"):
                continue  # echoes of our own sends, delivery/read events, etc.

            sender = event.get("sender", {}).get("id", "")
            if not sender or sender == account_id:
                continue

            if _seen(message.get("mid", "")):
                print(f"Duplicate ignored: {message.get('mid')}")
                continue

            text = (message.get("text") or "").strip()
            if not text:
                audio_url = _audio_attachment_url(message)
                if audio_url:
                    try:
                        text = whatsapp.transcribe_audio_url(audio_url)
                        print(f"[{owner['business_name']}] {channel.upper()} {sender} | Voice transcribed: {text}")
                    except Exception as e:
                        print(f"Transcription failed: {e}")
                        channels.send_text(owner, channel, sender,
                            "Sorry, I could not understand your voice message. Please try again or type your booking request.")
                        continue
                else:
                    channels.send_text(owner, channel, sender,
                        "Sorry, I can only handle text and voice messages here. Please type your booking request.")
                    continue
            else:
                print(f"[{owner['business_name']}] {channel.upper()} {sender} | Msg: {text}")
            reply = bot.chat(owner, sender, text, channel=channel)
            if reply:
                print(f"Reply: {reply}")
                channels.send_text(owner, channel, sender, reply)
