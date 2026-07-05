"""Text-to-speech replies via Groq (PlayAI voices).

Generated clips are kept in a small in-memory cache and served publicly from
/tts/{id}.mp3 so Meta's platforms can fetch them as audio attachments.
"""
import os
import uuid
from collections import OrderedDict

from groq import Groq

groq_client = Groq()

TTS_MODEL = os.getenv("TTS_MODEL", "canopylabs/orpheus-v1-english")
TTS_VOICE = os.getenv("TTS_VOICE", "hannah")

# id -> mp3 bytes; bounded so long-running processes don't grow unbounded
_audio_cache = OrderedDict()
_MAX_CLIPS = 100


def synthesize(text):
    """Generate an mp3 clip for the reply. Returns a cache id.

    Raises on failure (e.g. PlayAI model terms not yet accepted on the Groq
    console) — callers treat voice as best-effort and fall back to text.
    """
    # TTS reads plain sentences best; keep it well under the model's input cap
    speech_text = text.strip()[:2000]
    resp = groq_client.audio.speech.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=speech_text,
        response_format="mp3",
    )
    audio = resp.read() if hasattr(resp, "read") else resp.content

    clip_id = uuid.uuid4().hex
    _audio_cache[clip_id] = audio
    while len(_audio_cache) > _MAX_CLIPS:
        _audio_cache.popitem(last=False)
    return clip_id


def get_clip(clip_id):
    """mp3 bytes or None."""
    return _audio_cache.get(clip_id)
