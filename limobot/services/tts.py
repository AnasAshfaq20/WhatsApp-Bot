"""Text-to-speech replies via Groq (Orpheus).

Orpheus outputs WAV only; WhatsApp needs mp3/ogg, so clips are re-encoded to
mp3 with lameenc (pure wheel, no system ffmpeg needed). Generated clips are
kept in a small in-memory cache and served publicly from /tts/{id}.mp3 so
Meta's platforms can fetch them as audio attachments.
"""
import io
import os
import uuid
import wave
from collections import OrderedDict

import lameenc
from groq import Groq

groq_client = Groq()

TTS_MODEL = os.getenv("TTS_MODEL", "canopylabs/orpheus-v1-english")
TTS_VOICE = os.getenv("TTS_VOICE", "hannah")

# id -> mp3 bytes; bounded so long-running processes don't grow unbounded
_audio_cache = OrderedDict()
_MAX_CLIPS = 100


def _wav_to_mp3(wav_bytes):
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        channels = w.getnchannels()
        rate     = w.getframerate()
        width    = w.getsampwidth()
        pcm      = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"Expected 16-bit PCM, got {width * 8}-bit")
    enc = lameenc.Encoder()
    enc.set_bit_rate(64)
    enc.set_in_sample_rate(rate)
    enc.set_channels(channels)
    enc.set_quality(5)
    return bytes(enc.encode(pcm)) + bytes(enc.flush())


def synthesize(text):
    """Generate an mp3 clip for the reply. Returns a cache id.

    Raises on failure (e.g. model terms not yet accepted on the Groq console)
    — callers treat voice as best-effort and fall back to text.
    """
    # TTS reads plain sentences best; keep it well under the model's input cap
    speech_text = text.strip()[:2000]
    resp = groq_client.audio.speech.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=speech_text,
        response_format="wav",
    )
    wav = resp.read() if hasattr(resp, "read") else resp.content
    audio = _wav_to_mp3(wav)

    clip_id = uuid.uuid4().hex
    _audio_cache[clip_id] = audio
    while len(_audio_cache) > _MAX_CLIPS:
        _audio_cache.popitem(last=False)
    return clip_id


def get_clip(clip_id):
    """mp3 bytes or None."""
    return _audio_cache.get(clip_id)
