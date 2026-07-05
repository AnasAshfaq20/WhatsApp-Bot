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
# Optional Orpheus vocal direction, e.g. "warm" or "friendly". Groq's docs
# recommend no direction for the most natural customer-support cadence.
TTS_STYLE = os.getenv("TTS_STYLE", "")

# Model for rewriting chat text into natural spoken lines. Facts (prices,
# dates) must survive the rewrite, so use the main model, not a small one.
SPEECH_REWRITE_MODEL = os.getenv("SPEECH_REWRITE_MODEL", "openai/gpt-oss-120b")

SPEECHIFY_PROMPT = """You turn a written chat reply from a limousine booking assistant into what a warm, friendly human agent would SAY out loud in a short WhatsApp voice note.

ALWAYS rewrite fully in your own natural spoken words — never copy the written sentences. Any fact you do mention (vehicle names, dates, times, passenger counts, prices) must stay exactly the same: never recompute, round, or invent a number.

THE VOICE NOTE IS A COMPANION, NOT A NARRATION. The customer also receives the full text. Your job is what a human agent would quickly SAY, not a read-aloud of the message:
- If the written reply lists several options or a detailed breakdown, do NOT read them all. Summarize like a person would ("I've lined up a few options for you — the Escalade's the sweet spot at two twenty for the two hours — full details in the message") and move to the question.
- Keep it under 35 words total. Shorter is more human.

Style rules:
- Warm, casual, human. Use contractions. Vary sentence openings.
- It's a voice note in a chat, NOT a phone call — never say "thanks for calling".
- Absolutely no lists, symbols, or math notation — turn "$140 x 4 hours = $560" into "that's one forty an hour, so five hundred and sixty total for the four hours".
- Speak numbers and times naturally: "6:00 PM" -> "six in the evening", "8 July" -> "the eighth of July".
- Never read out reference codes or phone numbers digit by digit — say something like "your booking's confirmed, all the details are in the message" instead.
- At most three short sentences. If the written reply ends with a question, end by asking it naturally.
- Output ONLY the spoken words. No quotation marks.

Example 1:
Written: Noted - 5 passengers, Monday night, 6 July. What time would you like the vehicle to arrive for pickup?
Spoken: Perfect, so that's five of you on Monday night, the sixth of July. What time should the car come pick you up?

Example 2:
Written: Price: $110 x 4 hours = $440
Shall I confirm this booking? Please reply YES to confirm.
Spoken: So that's one ten an hour, coming to four hundred and forty dollars for the four hours. Shall I go ahead and confirm the booking for you?

Example 3:
Written: Good news! Your booking LX-0021 is CONFIRMED. Your chauffeur is Michael Brown (+15550002222). We look forward to serving you.
Spoken: Great news, your booking's confirmed! Michael Brown will be your chauffeur, and his number's right there in the message. We can't wait to have you on board.

Example 4:
Written: For 6 passengers you could choose:
Cadillac Escalade - capacity 6, $110 x 2 hrs = $220 (minimum 2 hrs)
GMC Yukon Denali - capacity 6, $100 x 2 hrs = $200 (minimum 2 hrs)
Lincoln Stretch Limousine - capacity 8, $140 x 3 hrs = $420 (minimum 3 hrs)
What is the pickup location?
Spoken: I've got a few great options for the six of you, starting around two hundred dollars — the details are in the message. Where should we pick you up?"""


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
    _audio_cache[clip_id] = audio
    while len(_audio_cache) > _MAX_CLIPS:
        _audio_cache.popitem(last=False)
    return clip_id


def get_clip(clip_id):
    """mp3 bytes or None."""
    return _audio_cache.get(clip_id)
