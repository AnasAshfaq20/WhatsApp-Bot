"""Unified outbound messaging across WhatsApp, Facebook Messenger and Instagram DM.

WhatsApp uses the per-owner Cloud API number (whatsapp_token + whatsapp_phone_id).
Messenger and Instagram both use the Send API with the owner's Facebook Page
token — Instagram DMs are sent through the page linked to the IG account.
"""
import requests

from .. import config
from . import whatsapp

WHATSAPP = "whatsapp"
FACEBOOK = "facebook"
INSTAGRAM = "instagram"


def _page_send(owner, recipient_id, message):
    """Messenger/Instagram Send API — same endpoint for both channels."""
    url = f"{config.GRAPH_API_BASE}/me/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "messaging_type": "RESPONSE",
        "message": message,
    }
    r = requests.post(url, params={"access_token": owner.get("fb_page_token") or ""},
                      json=payload)
    if not r.ok:
        print(f"Meta Send API error: {r.status_code} {r.text}")


def send_text(owner, channel, to, body):
    if channel in (FACEBOOK, INSTAGRAM):
        # Messenger caps a message at 2000 chars; split long confirmations
        for chunk in _split(body, 1900):
            _page_send(owner, to, {"text": chunk})
    else:
        whatsapp.send_text(owner, to, body)


def send_image(owner, channel, to, image_url, caption=""):
    if channel in (FACEBOOK, INSTAGRAM):
        _page_send(owner, to, {
            "attachment": {"type": "image", "payload": {"url": image_url, "is_reusable": True}}
        })
        if caption:
            _page_send(owner, to, {"text": caption})
    else:
        whatsapp.send_image(owner, to, image_url, caption)


def _split(text, limit):
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks
