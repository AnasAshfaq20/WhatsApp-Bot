import secrets
import string

from fastapi import APIRouter, Request, Body, Depends, UploadFile, File

from ..db import (create_owner, update_owner, delete_owner, get_all_owners,
                  get_owner_by_username, save_menu_image)
from ..templating import templates
from .auth import require_admin

ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
MAX_IMAGE_BYTES     = 5 * 1024 * 1024  # 5 MB

router = APIRouter()


def _generate_password(length=10):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@router.get("/admin")
def admin_panel(request: Request, session: dict = Depends(require_admin)):
    return templates.TemplateResponse(request, "admin.html", {})


@router.get("/admin/owners")
def list_owners(session: dict = Depends(require_admin)):
    return {"owners": get_all_owners()}


@router.post("/admin/owners")
def add_owner(payload: dict = Body(default={}), session: dict = Depends(require_admin)):
    username        = (payload.get("username") or "").strip().lower()
    owner_name      = (payload.get("owner_name") or "").strip()
    restaurant_name = (payload.get("restaurant_name") or "").strip()

    if not username or not owner_name or not restaurant_name:
        return {"success": False,
                "error": "username, owner_name and restaurant_name are required"}
    if get_owner_by_username(username):
        return {"success": False, "error": "Username already taken"}

    password = _generate_password()
    owner_id = create_owner(
        username          = username,
        password          = password,
        owner_name        = owner_name,
        restaurant_name   = restaurant_name,
        hours             = payload.get("hours", ""),
        location          = payload.get("location", ""),
        delivery_info     = payload.get("delivery_info", ""),
        whatsapp_token    = payload.get("whatsapp_token", ""),
        whatsapp_phone_id = payload.get("whatsapp_phone_id", ""),
        admin_phone       = payload.get("admin_phone", ""),
        menu_image_url    = payload.get("menu_image_url", ""),
        voice_phone       = payload.get("voice_phone", ""),
    )

    # Plaintext password is returned ONCE so the admin can share it
    return {"success": True, "owner_id": owner_id,
            "credentials": {"username": username, "password": password}}


@router.put("/admin/owners/{owner_id}")
def edit_owner(owner_id: int, payload: dict = Body(default={}),
               session: dict = Depends(require_admin)):
    update_owner(owner_id, payload)
    return {"success": True}


@router.post("/admin/owners/{owner_id}/reset-password")
def reset_owner_password(owner_id: int, session: dict = Depends(require_admin)):
    password = _generate_password()
    update_owner(owner_id, {"password": password})
    return {"success": True, "password": password}


@router.delete("/admin/owners/{owner_id}")
def remove_owner(owner_id: int, session: dict = Depends(require_admin)):
    delete_owner(owner_id)
    return {"success": True}


@router.post("/admin/owners/{owner_id}/menu-image")
def upload_menu_image(owner_id: int, request: Request,
                      image: UploadFile = File(None),
                      session: dict = Depends(require_admin)):
    if image is None or not image.filename:
        return {"success": False, "error": "No image file provided"}

    mime = image.content_type or ""
    if mime not in ALLOWED_IMAGE_TYPES:
        return {"success": False, "error": "Only PNG, JPG or WebP images are allowed"}

    data = image.file.read()
    if len(data) > MAX_IMAGE_BYTES:
        return {"success": False, "error": "Image too large (max 5 MB)"}

    # Public URL served by this app — WhatsApp fetches the image from here
    public_url = str(request.base_url).rstrip("/") + f"/menu-image/{owner_id}"
    save_menu_image(owner_id, data, mime, public_url)
    return {"success": True, "url": public_url}
