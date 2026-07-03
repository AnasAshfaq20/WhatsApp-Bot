import secrets
import string

from fastapi import APIRouter, Request, Body, Depends, UploadFile, File

from ..db import (create_owner, update_owner, delete_owner, get_all_owners,
                  get_owner_by_username, save_fleet_image,
                  get_vehicles_for_owner, add_vehicle, update_vehicle,
                  delete_vehicle)
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
    username      = (payload.get("username") or "").strip().lower()
    owner_name    = (payload.get("owner_name") or "").strip()
    business_name = (payload.get("business_name") or "").strip()

    if not username or not owner_name or not business_name:
        return {"success": False,
                "error": "username, owner_name and business_name are required"}
    if get_owner_by_username(username):
        return {"success": False, "error": "Username already taken"}

    password = _generate_password()
    owner_id = create_owner(
        username          = username,
        password          = password,
        owner_name        = owner_name,
        business_name     = business_name,
        hours             = payload.get("hours", ""),
        location          = payload.get("location", ""),
        service_area      = payload.get("service_area", ""),
        whatsapp_token    = payload.get("whatsapp_token", ""),
        whatsapp_phone_id = payload.get("whatsapp_phone_id", ""),
        admin_phone       = payload.get("admin_phone", ""),
        fleet_image_url   = payload.get("fleet_image_url", ""),
        voice_phone       = payload.get("voice_phone", ""),
        currency          = payload.get("currency", "$") or "$",
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


@router.post("/admin/owners/{owner_id}/fleet-image")
def upload_fleet_image(owner_id: int, request: Request,
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
    public_url = str(request.base_url).rstrip("/") + f"/fleet-image/{owner_id}"
    save_fleet_image(owner_id, data, mime, public_url)
    return {"success": True, "url": public_url}


# ── Fleet management ──
@router.get("/admin/owners/{owner_id}/vehicles")
def list_vehicles(owner_id: int, session: dict = Depends(require_admin)):
    return {"vehicles": get_vehicles_for_owner(owner_id)}


@router.post("/admin/owners/{owner_id}/vehicles")
def create_vehicle(owner_id: int, payload: dict = Body(default={}),
                   session: dict = Depends(require_admin)):
    name     = (payload.get("name") or "").strip()
    category = (payload.get("category") or "").strip()
    if not name or not category:
        return {"success": False, "error": "name and category are required"}
    try:
        vehicle_id = add_vehicle(
            owner_id     = owner_id,
            category     = category,
            name         = name,
            capacity     = int(payload.get("capacity") or 4),
            hourly_rate  = int(payload.get("hourly_rate") or 0),
            min_hours    = int(payload.get("min_hours") or 2),
            airport_rate = int(payload.get("airport_rate") or 0),
            description  = payload.get("description", ""),
        )
    except (TypeError, ValueError):
        return {"success": False, "error": "capacity and rates must be numbers"}
    return {"success": True, "vehicle_id": vehicle_id}


@router.put("/admin/owners/{owner_id}/vehicles/{vehicle_id}")
def edit_vehicle(owner_id: int, vehicle_id: int, payload: dict = Body(default={}),
                 session: dict = Depends(require_admin)):
    for key in ("capacity", "hourly_rate", "min_hours", "airport_rate"):
        if key in payload:
            try:
                payload[key] = int(payload[key])
            except (TypeError, ValueError):
                return {"success": False, "error": f"{key} must be a number"}
    update_vehicle(vehicle_id, owner_id, payload)
    return {"success": True}


@router.delete("/admin/owners/{owner_id}/vehicles/{vehicle_id}")
def remove_vehicle(owner_id: int, vehicle_id: int,
                   session: dict = Depends(require_admin)):
    delete_vehicle(vehicle_id, owner_id)
    return {"success": True}
