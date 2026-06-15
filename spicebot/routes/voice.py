"""
Voice agent webhook — called by the voice platform (e.g. Vapi) when a phone
order is confirmed on a call. The platform runs the spoken conversation; this
endpoint just persists the finished order, exactly like the WhatsApp flow.

Security: the platform must send the shared secret in the X-Voice-Secret header.
"""
import json

from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse

from .. import config
from ..db import save_order, get_owner_by_voice_phone, get_owner_by_id
from ..services import bot, whatsapp

router = APIRouter()


def _normalize_items(raw_items):
    """Coerce the platform's items array into our {name, qty, price} shape."""
    items = []
    for it in raw_items or []:
        name = it.get("name") or it.get("item") or ""
        qty  = it.get("qty") or it.get("quantity") or 1
        price = it.get("price") or 0
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            qty = 1
        try:
            price = int(float(price))
        except (TypeError, ValueError):
            price = 0
        if name:
            items.append({"name": name, "qty": qty, "price": price})
    return items


@router.post("/voice/order")
def voice_order(request: Request, payload: dict = Body(default={})):
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

    # ── Resolve the restaurant by the number the customer called ──
    called_number = (args.get("called_number") or args.get("to")
                     or payload.get("called_number") or "")
    owner = get_owner_by_voice_phone(called_number)
    if not owner and args.get("owner_id"):
        owner = get_owner_by_id(args["owner_id"])
    if not owner:
        print(f"Voice order: no owner for called_number={called_number!r}")
        return JSONResponse({"success": False, "error": "Restaurant not found for this number"},
                            status_code=404)

    # ── Build the order ──
    customer_phone = (args.get("customer_phone") or args.get("from") or "").strip()
    name    = (args.get("name") or "Guest").strip()
    address = (args.get("address") or "").strip()
    items   = _normalize_items(args.get("items"))

    if not items:
        return JSONResponse({"success": False, "error": "No items in the order"}, status_code=400)

    total = args.get("total")
    try:
        total = int(float(total))
    except (TypeError, ValueError):
        total = sum(i["qty"] * i["price"] for i in items)

    # ── Persist (shows up on the dashboard immediately) ──
    save_order(owner_id=owner["id"], phone=customer_phone, name=name,
               address=address, total=total, items=items)
    print(f"[{owner['restaurant_name']}] VOICE ORDER SAVED: {name} / Rs.{total} / {len(items)} items")

    order_data = {"name": name, "items": items, "total": total,
                  "address": address, "phone": customer_phone}

    # ── Notify owner + send the caller a WhatsApp bill (best-effort) ──
    try:
        bot.notify_owner(owner, order_data)
    except Exception as e:
        print(f"Voice: owner notify failed: {e}")

    if customer_phone:
        try:
            whatsapp.send_text(owner, customer_phone, bot.format_bill(owner, order_data))
        except Exception as e:
            print(f"Voice: customer bill failed: {e}")

    # Spoken confirmation the agent reads back to the caller
    spoken = (f"Your order is confirmed. {len(items)} items, total {total} rupees. "
              f"Estimated delivery 30 to 45 minutes. Thank you for ordering from "
              f"{owner['restaurant_name']}.")
    return {"success": True, "total": total, "message": spoken}
