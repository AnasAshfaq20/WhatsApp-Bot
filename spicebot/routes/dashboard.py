from fastapi import APIRouter, Request, Body, Depends
from fastapi.responses import JSONResponse, Response, PlainTextResponse

from ..db import (get_owner_by_id, get_orders_for_owner, update_order_status_db,
                  get_menu_image)
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


@router.get("/menu-image/{owner_id}")
def menu_image(owner_id: int):
    """Public — WhatsApp fetches the uploaded menu image from here."""
    img = get_menu_image(owner_id)
    if not img:
        return PlainTextResponse("Not found", status_code=404)
    data, mime = img
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "public, max-age=300"})


@router.get("/orders")
def view_orders(request: Request, session: dict = Depends(require_login)):
    owner_id = _current_owner_id(request)
    owner = get_owner_by_id(owner_id) if owner_id else None
    if not owner:
        return PlainTextResponse("Owner not found", status_code=404)
    return templates.TemplateResponse(request, "dashboard.html", {
        "restaurant_name": owner["restaurant_name"],
        "owner_id": owner["id"],
        "is_admin": session.get("role") == "admin",
    })


@router.get("/orders/data")
def orders_data(request: Request, session: dict = Depends(require_login)):
    owner_id = _current_owner_id(request)
    if not owner_id:
        return {"orders": []}
    return {"orders": get_orders_for_owner(owner_id)}


@router.post("/orders/update")
def update_order_status(request: Request, payload: dict = Body(default={}),
                        session: dict = Depends(require_login)):
    order_id   = payload.get("id")
    new_status = payload.get("status")

    # Owners can only touch their own orders; admin can touch any
    scope_owner_id = None if session.get("role") == "admin" else session.get("owner_id")

    try:
        order = update_order_status_db(order_id, new_status, owner_id=scope_owner_id)
        if not order:
            return {"success": False, "error": "Order not found"}

        owner = get_owner_by_id(order["owner_id"])
        try:
            if new_status == "preparing":
                whatsapp.send_text(owner, order["phone"],
                    "Your order is now being prepared. Estimated delivery: 30-45 minutes.")
            elif new_status == "delivered":
                whatsapp.send_text(owner, order["phone"],
                    f"Your order has been delivered. Enjoy your meal! Thank you for choosing {owner['restaurant_name']}.")
        except Exception as e:
            print(f"Notify failed: {e}")

        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/reset")
def reset_conversations():
    bot.clear_conversations()
    return {"status": "conversations cleared"}
