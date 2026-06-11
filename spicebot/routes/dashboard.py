from flask import Blueprint, request, render_template, jsonify, session

from ..db import (get_owner_by_id, get_orders_for_owner, update_order_status_db)
from ..services import bot, whatsapp

dashboard_bp = Blueprint("dashboard", __name__)

from .auth import login_required


def _current_owner_id():
    """Owner sees their own data; admin can view any owner via ?owner_id=."""
    if session.get("role") == "admin":
        return request.args.get("owner_id", type=int)
    return session.get("owner_id")


@dashboard_bp.route("/", methods=["GET"])
def health():
    return {"status": "ok"}


@dashboard_bp.route("/orders", methods=["GET"])
@login_required
def view_orders():
    owner_id = _current_owner_id()
    owner = get_owner_by_id(owner_id) if owner_id else None
    if not owner:
        return "Owner not found", 404
    return render_template("dashboard.html",
                           restaurant_name=owner["restaurant_name"],
                           owner_id=owner["id"],
                           is_admin=(session.get("role") == "admin"))


@dashboard_bp.route("/orders/data", methods=["GET"])
@login_required
def orders_data():
    owner_id = _current_owner_id()
    if not owner_id:
        return jsonify({"orders": []})
    return jsonify({"orders": get_orders_for_owner(owner_id)})


@dashboard_bp.route("/orders/update", methods=["POST"])
@login_required
def update_order_status():
    data       = request.get_json()
    order_id   = data.get("id")
    new_status = data.get("status")

    # Owners can only touch their own orders; admin can touch any
    scope_owner_id = None if session.get("role") == "admin" else session.get("owner_id")

    try:
        order = update_order_status_db(order_id, new_status, owner_id=scope_owner_id)
        if not order:
            return jsonify({"success": False, "error": "Order not found"})

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

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@dashboard_bp.route("/reset", methods=["GET"])
def reset_conversations():
    bot.clear_conversations()
    return {"status": "conversations cleared"}
