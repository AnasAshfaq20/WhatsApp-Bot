"""Text-to-speech replies via Groq (Orpheus).

Orpheus outputs WAV only; WhatsApp needs mp3/ogg, so clips are re-encoded to
mp3 with lameenc (pure wheel, no system ffmpeg needed). Generated clips are
kept in a small in-memory cache and served publicly from /tts/{id}.mp3 so
Meta's platforms can fetch them as audio attachments.
"""
import audioop
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
# Optional Orpheus vocal direction, e.g. "warm" or "friendly". Groq's docs
# recommend no direction for the most natural customer-support cadence.
TTS_STYLE = os.getenv("TTS_STYLE", "")

# Model for rewriting chat text into natural spoken lines. Facts (prices,
# dates) must survive the rewrite, so use the main model, not a small one.
SPEECH_REWRITE_MODEL = os.getenv("SPEECH_REWRITE_MODEL", "openai/gpt-oss-120b")

SPEECHIFY_PROMPT = """You are the VOICE of a limousine booking assistant. Turn the written chat reply into exactly what a warm, natural human agent would SAY out loud in a WhatsApp voice note.

THE CUSTOMER ONLY HEARS YOUR VOICE — they get no text version. So include EVERY piece of information from the written reply: every vehicle option, capacity, price, date, time and question. Never drop a detail and never say "the details are in the message".

Sound like a real person explaining, not a machine reading:
- Flowing conversational sentences with contractions and natural connectors ("so", "now", "alright", "and if you'd rather...").
- NEVER read options as a list. Weave them into speech the way a person compares things: "we've got the Cadillac Escalade at one ten an hour, or the Yukon Denali at an even hundred — both seat six comfortably. And if you want to arrive in real style, the stretch limousine seats eight at one forty an hour, though that one has a three hour minimum."
- Speak numbers, dates, times and prices the human way: "$110" -> "one ten" or "a hundred and ten dollars", "£400" -> "four hundred pounds", "9 PM" -> "nine in the evening", "8 July" -> "the eighth of July", "$140 x 4 hrs = $560" -> "one forty an hour, so five hundred and sixty total for the four hours", "£400 x 3 days = £1200" -> "four hundred pounds a day, so twelve hundred for the three days".
- Every fact VALUE must stay exactly correct — never recompute, round, or invent a number.
- Exception: booking reference codes and phone numbers — don't spell them out digit by digit; those are sent in writing separately.
- Keep it under 110 words. End with the reply's question, asked naturally.
- Output ONLY the spoken words. No quotation marks, no stage directions.

Example 1:
Written: Noted - 5 passengers, Monday night, 6 July. What time would you like the vehicle to arrive for pickup?
Spoken: Perfect, so that's five of you on Monday night, the sixth of July. What time should the car come pick you up?

Example 2:
Written: For 6 passengers you could choose:
Cadillac Escalade - capacity 6, $110 x 2 hrs = $220 (minimum 2 hrs)
GMC Yukon Denali - capacity 6, $100 x 2 hrs = $200 (minimum 2 hrs)
Lincoln Stretch Limousine - capacity 8, $140 x 3 hrs = $420 (minimum 3 hrs)
What is the pickup location?
Spoken: Alright, for the six of you I've got a couple of lovely SUVs — the Cadillac Escalade at one ten an hour, that's two twenty for the two hour minimum, or the Yukon Denali at an even hundred, so two hundred total. And if you want to make an entrance, the stretch limousine seats eight at one forty an hour with a three hour minimum, coming to four twenty. So, where should we pick you up?

Example 3:
Written: Booking summary:
Vehicle: Lincoln Stretch Limousine
Date & Time: 8 July 2026, 6:00 PM
Pickup: Pearl Continental Hotel
Drop-off: Marriott
Passengers: 6
Price: $140 x 4 hours = $560
Shall I confirm this booking? Please reply YES to confirm.
Spoken: Alright, quick recap — that's the stretch limousine for six of you on the eighth of July at six in the evening, picking up from the Pearl Continental and heading to the Marriott. At one forty an hour it comes to five hundred and sixty for the four hours. Happy for me to lock that in? Just say yes to confirm."""


def speechify(text):
    """Rewrite a chat reply into a natural spoken line. Falls back to the
    original text if the rewrite fails."""
    messages = [
        {"role": "system", "content": SPEECHIFY_PROMPT},
        {"role": "user", "content": text},
    ]
    # gpt-oss spends hidden reasoning tokens from the same budget — keep the
    # cap high and reasoning low so the spoken line never gets truncated
    try:
        try:
            resp = groq_client.chat.completions.create(
                model=SPEECH_REWRITE_MODEL, temperature=0.3, max_tokens=2048,
                reasoning_effort="low", messages=messages)
        except Exception:
            resp = groq_client.chat.completions.create(
                model=SPEECH_REWRITE_MODEL, temperature=0.3, max_tokens=2048,
                messages=messages)
        spoken = (resp.choices[0].message.content or "").strip().strip('"').strip()
        # A truncated or copied rewrite sounds worse than none — sanity checks
        if not spoken or spoken == text.strip():
            return text
        return spoken
    except Exception as e:
        print(f"Speechify failed, using literal text: {e}")
        return text

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
    # WhatsApp mobile is picky: the verified-working combination is a
    # 44.1 kHz stereo MPEG-1 mp3 delivered via media upload (not by link)
    if rate != 44100:
        pcm, _ = audioop.ratecv(pcm, width, channels, rate, 44100, None)
        rate = 44100
    if channels == 1:
        pcm = audioop.tostereo(pcm, width, 1, 1)
        channels = 2
    enc = lameenc.Encoder()
    enc.set_bit_rate(128)
    enc.set_in_sample_rate(rate)
    enc.set_channels(channels)
    enc.set_quality(2)
    return bytes(enc.encode(pcm)) + bytes(enc.flush())


def synthesize(text):
    """Generate an mp3 clip for the reply. Returns a cache id.

    Raises on failure (e.g. model terms not yet accepted on the Groq console)
    — callers treat voice as best-effort and fall back to text.
    """
    # TTS reads plain sentences best; keep it well under the model's input cap
    speech_text = text.strip()[:2000]
    if TTS_STYLE:
        speech_text = f"[{TTS_STYLE}] {speech_text}"
    resp = groq_client.audio.speech.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=speech_text,
        response_format="wav",
    )
    wav = resp.read() if hasattr(resp, "read") else resp.content
    audio = _wav_to_mp3(wav)

    clip_id = uuid.uuid4().hex
    # Keep both: WhatsApp/Messenger take mp3, Instagram only accepts wav/m4a
    _audio_cache[clip_id] = {"mp3": audio, "wav": wav}
    while len(_audio_cache) > _MAX_CLIPS:
        _audio_cache.popitem(last=False)
    return clip_id


def get_clip(clip_id, fmt="mp3"):
    """Audio bytes in the requested format, or None."""
    entry = _audio_cache.get(clip_id)
    return entry.get(fmt) if entry else None
