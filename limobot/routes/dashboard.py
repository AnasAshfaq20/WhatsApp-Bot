from fastapi import APIRouter, Request, Body, Depends
from fastapi.responses import RedirectResponse, Response, PlainTextResponse

from ..db import (get_owner_by_id, get_bookings_for_owner, update_booking_status_db,
                  get_fleet_image)
from ..services import bot, whatsapp
from ..templating import templates
from .auth import require_login

router = APIRouter()


def _current_owner_id(request: Request):
    """Owner sees their own data; admin can view any owner via ?owner_id=."""
    if request.session.get("role") == "admin":
        raw = request.query_params.get("owner_id")
        return int(raw) if raw and raw.isdigit() else None
    return request.session.get("owner_id")


@router.get("/")
def health():
    return {"status": "ok"}


@router.get("/fleet-image/{owner_id}")
def fleet_image(owner_id: int):
    """Public — WhatsApp fetches the uploaded fleet card image from here."""
    img = get_fleet_image(owner_id)
    if not img:
        return PlainTextResponse("Not found", status_code=404)
    data, mime = img
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "public, max-age=300"})


@router.get("/bookings")
def view_bookings(request: Request, session: dict = Depends(require_login)):
    owner_id = _current_owner_id(request)
    owner = get_owner_by_id(owner_id) if owner_id else None
    if not owner:
        return PlainTextResponse("Owner not found", status_code=404)
    return templates.TemplateResponse(request, "dashboard.html", {
        "business_name": owner["business_name"],
        "currency": owner.get("currency") or "$",
        "owner_id": owner["id"],
        "is_admin": session.get("role") == "admin",
    })


@router.get("/orders")
def orders_redirect():
    """Old bookmark from the restaurant era."""
    return RedirectResponse(url="/bookings", status_code=307)


@router.get("/bookings/data")
def bookings_data(request: Request, session: dict = Depends(require_login)):
    owner_id = _current_owner_id(request)
    if not owner_id:
        return {"bookings": []}
    return {"bookings": get_bookings_for_owner(owner_id)}


STATUS_MESSAGES = {
    "confirmed": "Good news! Your booking {ref} is CONFIRMED.{driver_line} "
                 "We look forward to serving you.",
    "en_route":  "Your chauffeur is on the way to your pickup location for booking {ref}."
                 "{driver_line} Please be ready.",
    "completed": "Your trip {ref} is complete. Thank you for riding with {business}! "
                 "We would love to serve you again.",
    "cancelled": "Your booking {ref} has been cancelled. If this is unexpected, "
                 "please contact us and we will help right away.",
}


@router.post("/bookings/update")
def update_booking_status(request: Request, payload: dict = Body(default={}),
                          session: dict = Depends(require_login)):
    booking_id   = payload.get("id")
    new_status   = payload.get("status")
    driver_name  = payload.get("driver_name")
    driver_phone = payload.get("driver_phone")

    if new_status not in {"pending", "confirmed", "en_route", "completed", "cancelled"}:
        return {"success": False, "error": "Invalid status"}

    # Owners can only touch their own bookings; admin can touch any
    scope_owner_id = None if session.get("role") == "admin" else session.get("owner_id")

    try:
        booking = update_booking_status_db(booking_id, new_status,
                                           owner_id=scope_owner_id,
                                           driver_name=driver_name,
                                           driver_phone=driver_phone)
        if not booking:
            return {"success": False, "error": "Booking not found"}

        owner = get_owner_by_id(booking["owner_id"])
        template = STATUS_MESSAGES.get(new_status)
        if template:
            driver_line = ""
            if booking.get("driver_name"):
                driver_line = f" Your chauffeur is {booking['driver_name']}"
                if booking.get("driver_phone"):
                    driver_line += f" (+{booking['driver_phone'].lstrip('+')})"
                driver_line += "."
            try:
                whatsapp.send_text(owner, booking["phone"], template.format(
                    ref=booking["ref"], business=owner["business_name"],
                    driver_line=driver_line))
            except Exception as e:
                print(f"Notify failed: {e}")

        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/reset")
def reset_conversations():
    bot.clear_conversations()
    return {"status": "conversations cleared"}
