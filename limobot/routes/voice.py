"""
Voice agent webhook — called by the voice platform (e.g. Vapi) when a phone
booking is confirmed on a call. The platform runs the spoken conversation; this
endpoint just persists the finished booking, exactly like the WhatsApp flow.

Security: the platform must send the shared secret in the X-Voice-Secret header.
"""
import json

from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse

from .. import config
from ..db import (save_booking, booking_ref, get_owner_by_voice_phone,
                  get_owner_by_id)
from ..services import bot, whatsapp

router = APIRouter()


def _to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


@router.post("/voice/booking")
def voice_booking(request: Request, payload: dict = Body(default={})):
    # ── Auth ──
    if request.headers.get("X-Voice-Secret") != config.VOICE_WEBHOOK_SECRET:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)

    # Vapi wraps tool-call args under message.toolCalls[].function.arguments,
    # but also supports a flat body. Accept both.
    args = payload
    if "message" in payload:
        try:
            tool_calls = payload["message"].get("toolCalls") or payload["message"].get("tool_calls") or []
            if tool_calls:
                fn_args = tool_calls[0]["function"]["arguments"]
                args = fn_args if isinstance(fn_args, dict) else json.loads(fn_args)
        except Exception as e:
            print(f"Voice payload parse note: {e}")

    # ── Resolve the company by the number the customer called ──
    called_number = (args.get("called_number") or args.get("to")
                     or payload.get("called_number") or "")
    owner = get_owner_by_voice_phone(called_number)
    if not owner and args.get("owner_id"):
        owner = get_owner_by_id(args["owner_id"])
    if not owner:
        print(f"Voice booking: no owner for called_number={called_number!r}")
        return JSONResponse({"success": False, "error": "Company not found for this number"},
                            status_code=404)

    # ── Build the booking ──
    customer_phone   = (args.get("customer_phone") or args.get("from") or "").strip()
    name             = (args.get("name") or "Guest").strip()
    vehicle          = (args.get("vehicle") or "").strip()
    booking_type     = (args.get("booking_type") or "hourly").strip().lower()
    pickup_location  = (args.get("pickup_location") or "").strip()
    dropoff_location = (args.get("dropoff_location") or "").strip()
    pickup_time      = (args.get("pickup_time") or "").strip()
    hours            = _to_int(args.get("hours"))
    days             = _to_int(args.get("days"))
    return_time      = (args.get("return_time") or "").strip()
    passengers       = _to_int(args.get("passengers"), default=1)
    occasion         = (args.get("occasion") or "").strip()
    total            = _to_int(args.get("total"))

    if not vehicle or not pickup_location or not pickup_time:
        return JSONResponse(
            {"success": False,
             "error": "vehicle, pickup_location and pickup_time are required"},
            status_code=400)

    # ── Persist (shows up on the dashboard immediately) ──
    booking_id = save_booking(
        owner_id=owner["id"], phone=customer_phone, name=name, vehicle=vehicle,
        booking_type=booking_type, pickup_location=pickup_location,
        dropoff_location=dropoff_location, pickup_time=pickup_time,
        hours=hours, passengers=passengers, occasion=occasion, total=total,
        channel="voice", days=days, return_time=return_time)
    ref = booking_ref(booking_id)
    print(f"[{owner['business_name']}] VOICE BOOKING SAVED: {ref} {name} / {vehicle} / {total}")

    booking = {"name": name, "vehicle": vehicle, "booking_type": booking_type,
               "pickup_location": pickup_location, "dropoff_location": dropoff_location,
               "pickup_time": pickup_time, "hours": hours, "days": days,
               "return_time": return_time, "passengers": passengers,
               "occasion": occasion, "total": total, "phone": customer_phone,
               "channel": "voice"}

    # ── Notify owner + send the caller a WhatsApp confirmation (best-effort) ──
    try:
        bot.notify_owner(owner, booking, ref)
    except Exception as e:
        print(f"Voice: owner notify failed: {e}")

    if customer_phone:
        try:
            whatsapp.send_text(owner, customer_phone,
                               bot.format_confirmation(owner, booking, ref))
        except Exception as e:
            print(f"Voice: customer confirmation failed: {e}")

    # Spoken confirmation the agent reads back to the caller
    cur = owner.get("currency") or "$"
    spoken = (f"Your booking is confirmed. Reference {ref}. {vehicle}, pickup at "
              f"{pickup_time} from {pickup_location}, total {cur}{total}. "
              f"Your chauffeur's details will be shared before pickup. "
              f"Thank you for choosing {owner['business_name']}.")
    return {"success": True, "reference": ref, "total": total, "message": spoken}


# Backwards-compatible alias for platforms still pointed at the old path
@router.post("/voice/order")
def voice_order_alias(request: Request, payload: dict = Body(default={})):
    return voice_booking(request, payload)
