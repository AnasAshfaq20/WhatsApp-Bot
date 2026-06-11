import secrets
import string

from flask import Blueprint, request, render_template, jsonify

from ..db import (create_owner, update_owner, delete_owner, get_all_owners,
                  get_owner_by_username, save_menu_image)
from .auth import admin_required

ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
MAX_IMAGE_BYTES     = 5 * 1024 * 1024  # 5 MB

admin_bp = Blueprint("admin", __name__)


def _generate_password(length=10):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@admin_bp.route("/admin", methods=["GET"])
@admin_required
def admin_panel():
    return render_template("admin.html")


@admin_bp.route("/admin/owners", methods=["GET"])
@admin_required
def list_owners():
    return jsonify({"owners": get_all_owners()})


@admin_bp.route("/admin/owners", methods=["POST"])
@admin_required
def add_owner():
    data = request.get_json() or {}

    username        = (data.get("username") or "").strip().lower()
    owner_name      = (data.get("owner_name") or "").strip()
    restaurant_name = (data.get("restaurant_name") or "").strip()

    if not username or not owner_name or not restaurant_name:
        return jsonify({"success": False,
                        "error": "username, owner_name and restaurant_name are required"})
    if get_owner_by_username(username):
        return jsonify({"success": False, "error": "Username already taken"})

    password = _generate_password()
    owner_id = create_owner(
        username          = username,
        password          = password,
        owner_name        = owner_name,
        restaurant_name   = restaurant_name,
        hours             = data.get("hours", ""),
        location          = data.get("location", ""),
        delivery_info     = data.get("delivery_info", ""),
        whatsapp_token    = data.get("whatsapp_token", ""),
        whatsapp_phone_id = data.get("whatsapp_phone_id", ""),
        admin_phone       = data.get("admin_phone", ""),
        menu_image_url    = data.get("menu_image_url", ""),
    )

    # Plaintext password is returned ONCE so the admin can share it
    return jsonify({"success": True, "owner_id": owner_id,
                    "credentials": {"username": username, "password": password}})


@admin_bp.route("/admin/owners/<int:owner_id>", methods=["PUT"])
@admin_required
def edit_owner(owner_id):
    data = request.get_json() or {}
    update_owner(owner_id, data)
    return jsonify({"success": True})


@admin_bp.route("/admin/owners/<int:owner_id>/reset-password", methods=["POST"])
@admin_required
def reset_owner_password(owner_id):
    password = _generate_password()
    update_owner(owner_id, {"password": password})
    return jsonify({"success": True, "password": password})


@admin_bp.route("/admin/owners/<int:owner_id>", methods=["DELETE"])
@admin_required
def remove_owner(owner_id):
    delete_owner(owner_id)
    return jsonify({"success": True})


@admin_bp.route("/admin/owners/<int:owner_id>/menu-image", methods=["POST"])
@admin_required
def upload_menu_image(owner_id):
    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify({"success": False, "error": "No image file provided"})

    mime = file.mimetype or ""
    if mime not in ALLOWED_IMAGE_TYPES:
        return jsonify({"success": False, "error": "Only PNG, JPG or WebP images are allowed"})

    data = file.read()
    if len(data) > MAX_IMAGE_BYTES:
        return jsonify({"success": False, "error": "Image too large (max 5 MB)"})

    # Public URL served by this app — WhatsApp fetches the image from here
    public_url = request.url_root.rstrip("/") + f"/menu-image/{owner_id}"
    save_menu_image(owner_id, data, mime, public_url)
    return jsonify({"success": True, "url": public_url})
