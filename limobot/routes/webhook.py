from fastapi import APIRouter, Request, Body
from fastapi.responses import PlainTextResponse

from .. import config
from ..db import get_owner_by_phone_id, get_owner_by_fb_page, get_owner_by_ig_account
from ..services import bot, whatsapp, channels, tts

router = APIRouter()

# Deduplication — Meta sometimes retries webhooks
processed_message_ids = set()

BUSY_REPLY = ("Sorry, we're handling a lot of messages right now. "
              "Please resend your message in a minute and I'll be right with you.")


def _public_base(request):
    """Public https base URL of this app (Meta must be able to fetch from it)."""
    base = str(request.base_url).rstrip("/")
    if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://"):]
    return base


def _send_voice_reply(owner, channel, recipient, reply, base_url):
    """Speak the full reply as a voice note. Returns True on success so the
    caller can fall back to text when voice fails."""
    try:
        spoken = tts.speechify(reply)
        print(f"Spoken version: {spoken}")
        clip_id = tts.synthesize(spoken)
        channels.send_voice_clip(owner, channel, recipient, clip_id, base_url)
        print(f"Voice reply sent ({channel})")
        return True
    except Exception as e:
        print(f"Voice reply failed, falling back to text: {e}")
        return False


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
def receive_webhook(request: Request, data: dict = Body(default={})):
    base_url = _public_base(request)
    try:
        obj = data.get("object", "")
        # WhatsApp payloads carry entry[].changes; be lenient if object is missing
        if obj == "whatsapp_business_account" or (
                not obj and data.get("entry", [{}])[0].get("changes")):
            _handle_whatsapp(data, base_url)
        elif obj == "page":
            _handle_page(data, channels.FACEBOOK, base_url)
        elif obj == "instagram":
            _handle_page(data, channels.INSTAGRAM, base_url)
        else:
            print(f"Unhandled webhook object type: {obj!r}")
    except Exception as e:
        print("Error processing webhook:", e)

    return PlainTextResponse("EVENT_RECEIVED")


# ── WhatsApp Cloud API ──
def _handle_whatsapp(data, base_url=""):
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

    was_voice = False
    if m_type == "audio":
        try:
            media_id     = msg["audio"]["id"]
            incoming_msg = whatsapp.transcribe_audio(owner, media_id)
            was_voice    = True
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

    try:
        reply = bot.chat(owner, sender, incoming_msg, channel=channels.WHATSAPP)
    except Exception as e:
        # Never go silent — rate limits and hiccups get a polite retry ask
        print(f"bot.chat failed: {str(e)[:200]}")
        whatsapp.send_text(owner, sender, BUSY_REPLY)
        return
    if reply:
        print(f"Reply: {reply}")
        # Voice in -> voice-only out (full details spoken); text is the fallback
        voice_sent = (was_voice and base_url and
                      _send_voice_reply(owner, channels.WHATSAPP, sender, reply, base_url))
        if not voice_sent:
            whatsapp.send_text(owner, sender, reply)


# ── Facebook Messenger + Instagram DM (same event shape) ──
def _audio_attachment_url(message):
    """CDN URL of the first audio attachment on a Messenger/Instagram message."""
    for att in message.get("attachments") or []:
        if att.get("type") == "audio":
            return (att.get("payload") or {}).get("url")
    return None


def _handle_page(data, channel, base_url=""):
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

            was_voice = False
            text = (message.get("text") or "").strip()
            if not text:
                audio_url = _audio_attachment_url(message)
                if audio_url:
                    try:
                        text = whatsapp.transcribe_audio_url(audio_url)
                        was_voice = True
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
            try:
                reply = bot.chat(owner, sender, text, channel=channel)
            except Exception as e:
                print(f"bot.chat failed: {str(e)[:200]}")
                channels.send_text(owner, channel, sender, BUSY_REPLY)
                continue
            if reply:
                print(f"Reply: {reply}")
                # Voice in -> voice-only out (full details spoken); text is the fallback
                voice_sent = (was_voice and base_url and
                              _send_voice_reply(owner, channel, sender, reply, base_url))
                if not voice_sent:
                    channels.send_text(owner, channel, sender, reply)
