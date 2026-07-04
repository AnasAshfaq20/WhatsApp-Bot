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


def _page_send(owner, channel, recipient_id, message):
    """Send API for Messenger and Instagram.

    Messenger (and IG accounts linked via a Facebook Page) go through
    graph.facebook.com with the Page token. Accounts connected with
    "Instagram Login" have their own IGAA... token and use graph.instagram.com.
    """
    ig_token = owner.get("ig_token") or ""
    if channel == INSTAGRAM and ig_token:
        url = "https://graph.instagram.com/v19.0/me/messages"
        token = ig_token
    else:
        url = f"{config.GRAPH_API_BASE}/me/messages"
        token = owner.get("fb_page_token") or ""
    payload = {
        "recipient": {"id": recipient_id},
        "messaging_type": "RESPONSE",
        "message": message,
    }
    r = requests.post(url, params={"access_token": token}, json=payload)
    if not r.ok:
        print(f"Meta Send API error ({channel}): {r.status_code} {r.text}")


def send_text(owner, channel, to, body):
    if channel in (FACEBOOK, INSTAGRAM):
        # Messenger caps a message at 2000 chars; split long confirmations
        for chunk in _split(body, 1900):
            _page_send(owner, channel, to, {"text": chunk})
    else:
        whatsapp.send_text(owner, to, body)


def send_image(owner, channel, to, image_url, caption=""):
    if channel in (FACEBOOK, INSTAGRAM):
        _page_send(owner, channel, to, {
            "attachment": {"type": "image", "payload": {"url": image_url, "is_reusable": True}}
        })
        if caption:
            _page_send(owner, channel, to, {"text": caption})
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
