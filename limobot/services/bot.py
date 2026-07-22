"""Conversation engine: system prompt, LLM, booking extraction, notifications."""
import json
import os
import time
from datetime import datetime, timedelta

from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from ..db import (get_fleet_dict, save_booking, booking_ref,
                  save_chat_message, get_chat_history, clear_chat_history)
from ..config import now_pkt
from . import whatsapp, channels

llm = ChatGroq(model="openai/gpt-oss-120b", temperature=0.2)
# Separate quota pools on Groq — rescue conversations when the primary model
# hits its rate limit (429), which otherwise silently killed replies
fallback_llm = ChatGroq(model=os.getenv("FALLBACK_MODEL", "llama-3.3-70b-versatile"),
                        temperature=0.2)
# Last resort: the 8B model has by far the largest free-tier quota
emergency_llm = ChatGroq(model=os.getenv("EMERGENCY_MODEL", "llama-3.1-8b-instant"),
                         temperature=0.2)

# Free-tier friendliness: only the system prompt + the most recent turns are
# sent to the model. Long chats otherwise resend everything each message.
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "15"))


def _context_window(history):
    if len(history) <= MAX_HISTORY_MESSAGES + 1:
        return history
    return [history[0]] + history[-MAX_HISTORY_MESSAGES:]


def _invoke(history):
    """LLM call that survives rate limits: retry primary once, then fall back
    through the secondary and emergency models. Raises only if everything fails."""
    window = _context_window(history)
    last_err = None
    for attempt, model in enumerate((llm, llm, fallback_llm, emergency_llm), start=1):
        try:
            if attempt == 2:
                time.sleep(2)
            reply = model.invoke(window).content
            if reply and reply.strip():
                return reply
            last_err = ValueError("empty LLM reply")
            print(f"LLM attempt {attempt}: empty reply")
        except Exception as e:
            last_err = e
            print(f"LLM attempt {attempt} failed: {str(e)[:200]}")
    raise last_err

# In-memory conversation state, keyed by (owner_id, customer_phone)
conversations = {}


def build_system_prompt(owner):
    if (owner.get("bot_type") or "fleet") == "enquiry":
        return _enquiry_prompt(owner)
    return _fleet_prompt(owner)


def _enquiry_prompt(owner):
    name  = owner["business_name"]
    now   = now_pkt()
    today = now.strftime("%A, %d %B %Y")
    return f"""You are the professional WhatsApp assistant for "{name}".

TODAY'S DATE: {today}.

BUSINESS KNOWLEDGE (answer ONLY from this — never invent services, prices, clients or facts):
{owner.get('knowledge', '')}

YOUR JOB:
- Welcome visitors warmly and professionally
- Answer questions about the firm, its services, industries and locations using the knowledge above
- Understand what the visitor needs and guide them toward booking a consultation
- Capture a consultation enquiry: their full name, company, the service or area they need help with, and their preferred contact (email or phone) — plus any preferred time for a call

GREETING — FIRST MESSAGE ONLY:
- If the visitor opens with just a greeting, reply warmly: greet back, one or two lines on what {name} does, then "How can I help you today?"
- If their first message already states a need, respond to it directly.

ONE QUESTION AT A TIME — THIS IS CRITICAL:
- Every reply contains AT MOST ONE question. End the reply right after that question.
- Never ask for two or more details in the same message. Never send a list of questions.
- If the visitor volunteers several details at once, accept them all and ask only for the next single missing detail.

CONVERSATION RULES:
1. Keep replies SHORT — this is WhatsApp. Use line breaks, not paragraphs.
2. No emojis. No markdown formatting — no asterisks, underscores, bullets or numbered lists. Plain text only.
3. Re-read the conversation before asking anything; never re-ask a detail already given.
4. If asked something not covered by the knowledge (exact pricing, specific availability), say the team will cover it on the consultation call — and offer to arrange one.
5. CONFIRMATION IS MANDATORY. Once you have their name, company, area of interest and contact details, show a short summary and ask: "Shall I submit this consultation request? Please reply YES to confirm."
6. ONLY after an explicit yes, end your reply with this exact tag on its own line:
   [ENQUIRY_CAPTURED]
   Followed by JSON like:
   {{"name": "Sarah Ahmed", "company": "Falcon Capital", "service": "Fund Administration", "contact": "sarah@falconcap.com", "preferred_time": "Tuesday morning", "details": "Setting up a new ADGM fund, needs NAV and investor reporting"}}
   Never output the tag in the same message where you ask for confirmation.
7. If the user is rude or off-topic, respond politely that you can only help with {name} enquiries.
8. If asked who you are: "I am the {name} assistant. I can tell you about our services and arrange a consultation."

Stay strictly focused on {name} and its services."""


def _fleet_prompt(owner):
    fleet      = get_fleet_dict(owner)
    fleet_text = json.dumps(fleet, indent=2)
    name       = fleet["business_name"]
    cur        = fleet["currency"]
    now        = now_pkt()
    today      = now.strftime("%A, %d %B %Y")
    # LLMs are unreliable at weekday arithmetic — give them a lookup table
    calendar   = "\n".join(
        (now + timedelta(days=i)).strftime("- %A %d %B %Y") for i in range(15))
    return f"""You are the professional WhatsApp booking assistant for "{name}", a chauffeured limousine and luxury car rental service.

TODAY'S DATE: {today}.

CALENDAR — the next two weeks (use ONLY this table to resolve dates like "tomorrow", "this Friday" or "next Monday"; never guess a weekday):
{calendar}

YOUR JOB:
- Greet customers warmly and professionally
- Help them choose the right vehicle for their trip and group size
- Collect all booking details step by step
- Quote exact prices from the fleet list
- Confirm bookings only after explicit customer approval
- Answer questions about availability hours, service area and pricing

COMPANY INFO:
- Name: {name}
- Availability: {fleet['hours']}
- Base location: {fleet['location']}
- Service area: {fleet['service_area']}

FLEET (use these exact rates — never invent prices or vehicles):
{fleet_text}

PRICING RULES:
- Three booking types: "hourly" (chauffeur by the hour), "transfer" (flat-rate one-way AIRPORT pickup/drop-off) and "daily" (hire by the day, with a return date).
- Hourly: total = hourly_rate x hours. Each vehicle has min_hours — never quote fewer hours than that; tell the customer the minimum if they ask for less.
- Airport transfer: use the vehicle's airport_rate as the flat price. If airport_rate is null, that vehicle is not offered for transfers — suggest one that is.
- Daily hire: total = daily_rate x days. Count days from pickup date to return date (a same-day hire is 1 day; partial days round up). If daily_rate is null that vehicle is not offered for daily hire.
- Choose the booking type from what the customer wants: "hire a car for the weekend / until Sunday" is daily; "pick me up from Heathrow" is a transfer; "for the evening / 4 hours" is hourly.
- One-way trips that do NOT involve an airport (e.g. hotel to venue, house to another area) are booked as "hourly" at the vehicle's minimum hours. Say it simply, e.g. "One-way trips within the city are covered by our {cur}85 x 2 hours minimum = {cur}170 package" — do not ask how many hours for a simple one-way drop unless they want the car to wait.
- Always show the price breakdown before asking for confirmation, e.g. "{cur}110 x 4 hours = {cur}440" or "{cur}400 x 3 days = {cur}1200".

GREETING — FIRST MESSAGE ONLY:
- When the customer opens with just a greeting (hi, hello, salam, hey...), do NOT ask for booking details yet. Reply warmly and like a human, in this shape:
  Line 1: a warm greeting back, e.g. "Hello! Welcome to {name}."
  Line 2-3: a friendly one-or-two line intro — we provide chauffeur-driven luxury cars for airport transfers, weddings, corporate travel, nights out and more, with professional drivers, hourly hire or flat-rate transfers.
  Last line: "How can I help you today?"
- Match the customer's greeting naturally (reply to "good morning" with "Good morning!", to "salam" with "Walaikum Assalam!").
- The intro is ONLY for messages that are nothing but a greeting. If the first message contains ANY request or trip detail (a vehicle need, destination, date, group size — e.g. "hi, I need a car for six people tomorrow"), do NOT send the intro: greet in a word or two and address their request immediately, acknowledging the details they gave.

RESPOND INTELLIGENTLY, NOT LIKE A FORM:
- React to what the customer actually says; never interrogate through a fixed script.
- Before asking ANYTHING, re-read the whole conversation and list to yourself what is already known. NEVER re-ask a detail the customer has already given, even if their answer was vague or you asked it in different words.
- Accept vague answers and move on: "travelling", "touring", "going somewhere" simply means a point-to-point trip — that IS the occasion, do not ask for the occasion again.
- The occasion is optional. For airport runs and simple point-to-point trips, skip it entirely.
- When the customer answers, acknowledge briefly by restating the noted details in a few words before your next question, e.g. "Noted - 8 July, 6 PM, DHA Phase 8 to Gulberg. How many passengers will be traveling?" This keeps the booking on track.

BOOKING DETAILS TO COLLECT (skip anything already known):
1. Occasion / trip type — only if not already clear from the conversation
2. Pickup date and time
3. Pickup location (address or postcode)
4. Depending on the type: drop-off location (transfers) / number of hours (hourly) / return date and drop-off location (daily hire)
5. Number of passengers — then recommend the best-fitting vehicles with prices. Never book a vehicle with capacity below the passenger count.
6. Customer's full name — ask only at the end, right before confirmation.

ONE QUESTION AT A TIME — THIS IS CRITICAL:
- Every reply contains AT MOST ONE question. End the reply right after that question — no filler, no notes, no commentary about waiting.
- Never ask for two or more details in the same message. Never send a list of questions.
- If the customer volunteers several details in one message, accept them all and ask only for the next single missing detail.

CONVERSATION RULES:
1. Keep replies SHORT — this is WhatsApp, not email. Use line breaks, not paragraphs.
2. Do not use any emojis in your replies.
3. Do NOT use any markdown formatting — no asterisks (*), no underscores (_), no hyphens for bullets, no numbered lists, no bold, no italic. Plain text only.
4. Recommend vehicles that fit the passenger count and occasion; mention capacity and rate for each option (2-3 options max).
5. CONFIRMATION IS MANDATORY. Once you have all details, show a full booking summary (vehicle, date and time, pickup, drop-off or hours, passengers, price breakdown, total, customer name) and ask: "Shall I confirm this booking? Please reply YES to confirm." NEVER confirm until the customer explicitly says yes/confirm/book it (or similar) in a separate message. If they change something, update and ask again. Do NOT treat the message that provides their name as confirmation.
6. ONLY after the customer has explicitly confirmed in rule 5, end your reply with this exact tag on its own line:
   [BOOKING_CONFIRMED]
   Followed by a JSON block like:
   {{"name": "John Smith", "vehicle": "Black Limo 8-Seater", "booking_type": "hourly", "pickup_location": "...", "dropoff_location": "...", "pickup_time": "Saturday 15 Mar 2026, 7:00 PM", "hours": 4, "days": 0, "return_time": "", "passengers": 6, "occasion": "night out", "total": 440}}
   For transfers set "hours" to 0 and always fill "dropoff_location". For daily hire set "hours" to 0 and fill "days" and "return_time" (e.g. "Sunday 17 Mar 2026, 6:00 PM"). Never output [BOOKING_CONFIRMED] in the same message where you ask for confirmation.
7. If the customer asks for a vehicle you do not have, say it is unavailable and suggest the closest match from the fleet.
8. Currency: show prices as "{cur}440" format.
9. If the user sends rude, abusive, or offensive messages, respond with exactly: "Sorry, I can only assist with vehicle bookings. Please keep the conversation respectful."
10. If the user asks who you are, say: "I am the {name} booking assistant. I can help you reserve a chauffeured vehicle."
11. If the user asks unrelated questions (politics, general knowledge, other companies), respond with: "I can only help with {name} bookings. Would you like to see our fleet?"
12. Remember the booking details already collected — never ask for the same thing twice; show a running summary when details change.
13. Whenever you want to show the fleet to the customer (they ask for it, say yes to seeing it, or ask what cars are available), output the tag [SEND_FLEET] on its own line. Do NOT list the whole fleet in text — the fleet card image will be sent automatically.
14. UPSELLING: mention at most one relevant upgrade when it genuinely fits, e.g. suggest the stretch limousine for weddings or proms, or the Sprinter for groups near an SUV's capacity limit. One short line only.
15. Cancellation or changes to an existing booking: tell them a team member will contact them shortly, and continue helping with anything else.

Stay strictly focused on chauffeur and vehicle bookings for {name} only."""


def load_thread(owner, customer_phone):
    """Persistent WhatsApp-style thread: a fresh system prompt (current date,
    current fleet/knowledge) plus the stored recent conversation from the DB.
    Survives restarts and multi-day gaps."""
    messages = [SystemMessage(content=build_system_prompt(owner))]
    for row in get_chat_history(owner["id"], customer_phone,
                                limit=MAX_HISTORY_MESSAGES):
        cls = HumanMessage if row["role"] == "human" else AIMessage
        messages.append(cls(content=row["content"]))
    return messages


def clear_conversations():
    conversations.clear()
    try:
        clear_chat_history()
    except Exception as e:
        print(f"clear_chat_history failed: {e}")


def _enforce_single_question(reply):
    """The LLM occasionally crams its whole checklist into one message
    ("Where is pickup?Where is drop-off?How many passengers?..."). Keep
    everything up to and including the FIRST question only."""
    if reply.count("?") <= 1:
        return reply
    return reply[:reply.index("?") + 1].strip()


def chat(owner, customer_phone, incoming_msg, channel="whatsapp"):
    """Run one turn of the conversation. Returns the reply text (may be empty).

    customer_phone is the sender id on the channel: a phone number on WhatsApp,
    a PSID on Messenger, an IGSID on Instagram.
    """
    history = load_thread(owner, customer_phone)
    history.append(HumanMessage(content=incoming_msg))

    reply = _invoke(history)
    if "[BOOKING_CONFIRMED]" not in reply and "[SEND_FLEET]" not in reply:
        reply = _enforce_single_question(reply)

    # Fleet card request
    if "[SEND_FLEET]" in reply:
        if owner.get("fleet_image_url"):
            channels.send_image(owner, channel, customer_phone, owner["fleet_image_url"],
                                "Here is our fleet. Which vehicle would you like to book?")
            reply = reply.replace("[SEND_FLEET]", "").strip()
        else:
            # No image configured — let the LLM list the fleet in text instead
            history.append(AIMessage(content=reply))
            history.append(HumanMessage(
                content="(No fleet image is available. List the fleet in plain text: "
                        "vehicle, capacity and hourly rate per category. "
                        "Do not output the [SEND_FLEET] tag.)"))
            reply = _invoke(history).replace("[SEND_FLEET]", "").strip()

    reply = _extract_and_log_booking(owner, reply, customer_phone, channel)

    # Persist the turn — what the customer sent, and what they actually saw
    try:
        save_chat_message(owner["id"], customer_phone, "human", incoming_msg)
        save_chat_message(owner["id"], customer_phone, "ai",
                          reply or "(confirmation receipt sent)")
    except Exception as e:
        print(f"chat persistence failed: {e}")

    return reply


SERVICE_LABELS = {"transfer": "Flat-rate transfer", "daily": "Daily hire",
                  "hourly": "Hourly charter"}


def format_confirmation(owner, booking, ref):
    cur = owner.get("currency") or "$"
    btype = booking.get("booking_type", "hourly")
    lines = [
        "BOOKING CONFIRMED",
        "=" * 28,
        f"Reference: {ref}",
        f"Company: {owner['business_name']}",
        f"Customer: {booking.get('name', 'Guest')}",
        "-" * 28,
        f"Vehicle: {booking.get('vehicle', '')}",
        f"Service: {SERVICE_LABELS.get(btype, 'Hourly charter')}",
        f"Pickup: {booking.get('pickup_time', '')}",
        f"From: {booking.get('pickup_location', '')}",
    ]
    if booking.get("dropoff_location"):
        lines.append(f"To: {booking['dropoff_location']}")
    if btype == "daily":
        if booking.get("return_time"):
            lines.append(f"Return: {booking['return_time']}")
        if booking.get("days"):
            lines.append(f"Duration: {booking['days']} day{'s' if booking['days'] > 1 else ''}")
    elif btype != "transfer" and booking.get("hours"):
        lines.append(f"Duration: {booking['hours']} hours")
    lines += [
        f"Passengers: {booking.get('passengers', 1)}",
        "-" * 28,
        f"TOTAL: {cur}{booking['total']}",
        "=" * 28,
    ]
    deposit = owner.get("deposit_amount") or 0
    if deposit:
        lines.append(f"A {cur}{deposit} deposit is required to secure the vehicle.")
        if owner.get("payment_link"):
            lines.append(f"Payment link: {owner['payment_link']}")
        lines.append("-" * 28)
    lines += [
        "Your chauffeur's details will be shared before pickup.",
        f"Thank you for choosing {owner['business_name']}!",
    ]
    return "\n".join(lines)


def notify_owner(owner, booking, ref):
    if not owner.get("admin_phone"):
        return
    cur = owner.get("currency") or "$"
    lines = [
        "NEW BOOKING RECEIVED",
        "=" * 28,
        f"Reference: {ref}",
        f"Channel: {booking.get('channel', 'whatsapp').title()}",
        f"Received: {now_pkt().strftime('%d %b %Y, %I:%M %p')}",
        f"Customer: {booking.get('name', 'Guest')} (+{booking['phone']})",
        "-" * 28,
        f"Vehicle: {booking.get('vehicle', '')}",
        f"Type: {booking.get('booking_type', 'hourly')}",
        f"Occasion: {booking.get('occasion', '') or 'N/A'}",
        f"Pickup: {booking.get('pickup_time', '')}",
        f"From: {booking.get('pickup_location', '')}",
        f"To: {booking.get('dropoff_location', '') or 'N/A'}",
        f"Return: {booking.get('return_time', '') or 'N/A'}",
        f"Duration: {str(booking.get('days')) + ' days' if booking.get('days') else str(booking.get('hours') or 'N/A') + ' hours' if booking.get('hours') else 'N/A'}",
        f"Passengers: {booking.get('passengers', 1)}",
        "-" * 28,
        f"TOTAL: {cur}{booking['total']}",
        "=" * 28,
        "Open the dashboard to confirm and assign a chauffeur.",
    ]
    whatsapp.send_text(owner, owner["admin_phone"], "\n".join(lines))
    print(f"Owner notified at {owner['admin_phone']}")


def format_enquiry_confirmation(owner, enquiry, ref):
    lines = [
        "CONSULTATION REQUEST RECEIVED",
        "=" * 28,
        f"Reference: {ref}",
        f"Name: {enquiry.get('name', '')}",
    ]
    if enquiry.get("company"):
        lines.append(f"Company: {enquiry['company']}")
    lines += [
        f"Area: {enquiry.get('service', '')}",
        f"Contact: {enquiry.get('contact', '')}",
    ]
    if enquiry.get("preferred_time"):
        lines.append(f"Preferred time: {enquiry['preferred_time']}")
    lines += [
        "=" * 28,
        f"Thank you — the {owner['business_name']} team will be in touch shortly.",
    ]
    return "\n".join(lines)


def _extract_and_log_enquiry(owner, reply_text, customer_phone, channel):
    try:
        parts       = reply_text.split("[ENQUIRY_CAPTURED]")
        clean_reply = parts[0].strip()
        json_part   = parts[1].strip().replace("```json", "").replace("```", "").strip()
        enquiry     = json.loads(json_part)

        booking_id = save_booking(
            owner_id         = owner["id"],
            phone            = customer_phone,
            name             = enquiry.get("name", "Guest"),
            vehicle          = enquiry.get("service", "General enquiry"),
            booking_type     = "enquiry",
            pickup_location  = enquiry.get("contact", ""),
            dropoff_location = enquiry.get("company", ""),
            pickup_time      = enquiry.get("preferred_time", "") or "ASAP",
            hours            = 0,
            passengers       = 1,
            occasion         = enquiry.get("details", ""),
            total            = 0,
            channel          = channel,
        )
        ref = booking_ref(booking_id)
        print(f"ENQUIRY SAVED TO DB: {ref} {enquiry}")

        channels.send_text(owner, channel, customer_phone,
                           format_enquiry_confirmation(owner, enquiry, ref))
        try:
            if owner.get("admin_phone"):
                whatsapp.send_text(owner, owner["admin_phone"],
                    f"NEW CONSULTATION ENQUIRY {ref}\n"
                    f"Name: {enquiry.get('name', '')} ({enquiry.get('company', '') or 'no company'})\n"
                    f"Area: {enquiry.get('service', '')}\n"
                    f"Contact: {enquiry.get('contact', '')}\n"
                    f"Details: {enquiry.get('details', '') or 'N/A'}")
        except Exception as e:
            print(f"Owner notify failed: {e}")

        return clean_reply
    except Exception as e:
        print(f"Enquiry parse failed: {e}")
        return reply_text.split("[ENQUIRY_CAPTURED]")[0].strip()


def _extract_and_log_booking(owner, reply_text, customer_phone, channel="whatsapp"):
    if "[ENQUIRY_CAPTURED]" in reply_text:
        return _extract_and_log_enquiry(owner, reply_text, customer_phone, channel)
    if "[BOOKING_CONFIRMED]" not in reply_text:
        return reply_text
    try:
        parts       = reply_text.split("[BOOKING_CONFIRMED]")
        clean_reply = parts[0].strip()
        json_part   = parts[1].strip().replace("```json", "").replace("```", "").strip()

        booking = json.loads(json_part)
        booking["phone"] = customer_phone
        booking["channel"] = channel

        booking_id = save_booking(
            owner_id         = owner["id"],
            phone            = customer_phone,
            name             = booking.get("name", "Guest"),
            vehicle          = booking.get("vehicle", ""),
            booking_type     = booking.get("booking_type", "hourly"),
            pickup_location  = booking.get("pickup_location", ""),
            dropoff_location = booking.get("dropoff_location", ""),
            pickup_time      = booking.get("pickup_time", ""),
            hours            = int(booking.get("hours") or 0),
            passengers       = int(booking.get("passengers") or 1),
            occasion         = booking.get("occasion", ""),
            total            = int(booking.get("total") or 0),
            channel          = channel,
            days             = int(booking.get("days") or 0),
            return_time      = booking.get("return_time", ""),
        )
        ref = booking_ref(booking_id)
        print(f"BOOKING SAVED TO DB: {ref} {booking}")

        channels.send_text(owner, channel, customer_phone,
                           format_confirmation(owner, booking, ref))

        try:
            notify_owner(owner, booking, ref)
        except Exception as e:
            print(f"Owner notify failed: {e}")

        return clean_reply

    except Exception as e:
        print(f"Booking parse failed: {e}")
        return reply_text.split("[BOOKING_CONFIRMED]")[0].strip()
