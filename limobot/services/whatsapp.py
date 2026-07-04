"""Meta WhatsApp Cloud API — all calls are per-owner (token + phone ID)."""
import requests
from groq import Groq

from .. import config

groq_client = Groq()


def _messages_url(owner):
    return f"{config.GRAPH_API_BASE}/{owner['whatsapp_phone_id']}/messages"


def _headers(owner):
    return {
        "Authorization": f"Bearer {owner['whatsapp_token']}",
        "Content-Type": "application/json",
    }


def send_text(owner, to, body):
    payload = {
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "text",
        "text": {"body": body},
    }
    r = requests.post(_messages_url(owner), headers=_headers(owner), json=payload)
    if not r.ok:
        print(f"Meta API error: {r.status_code} {r.text}")


def send_image(owner, to, image_url, caption=""):
    payload = {
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "image",
        "image": {"link": image_url, "caption": caption},
    }
    r = requests.post(_messages_url(owner), headers=_headers(owner), json=payload)
    if not r.ok:
        print(f"Meta API image error: {r.status_code} {r.text}")


def transcribe_audio(owner, media_id):
    """Download a WhatsApp voice note and transcribe it via Groq Whisper."""
    headers = {"Authorization": f"Bearer {owner['whatsapp_token']}"}

    url_resp = requests.get(f"{config.GRAPH_API_BASE}/{media_id}", headers=headers)
    if not url_resp.ok:
        raise Exception(f"Failed to get media URL: {url_resp.text}")
    download_url = url_resp.json()["url"]

    audio_resp = requests.get(download_url, headers=headers)
    if not audio_resp.ok:
        raise Exception(f"Failed to download audio: {audio_resp.text}")

    return _whisper(audio_resp.content)


def transcribe_audio_url(audio_url):
    """Transcribe a voice note from a direct CDN URL (Messenger/Instagram attachments)."""
    audio_resp = requests.get(audio_url, timeout=30)
    if not audio_resp.ok:
        raise Exception(f"Failed to download audio: {audio_resp.status_code}")
    return _whisper(audio_resp.content)


def _whisper(audio_bytes):
    transcription = groq_client.audio.transcriptions.create(
        file=("voice.ogg", audio_bytes, "audio/ogg"),
        model="whisper-large-v3",
        language="en",
    )
    return transcription.text.strip()
