from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from werkzeug.security import check_password_hash

from .. import config
from ..db import get_owner_by_username
from ..templating import templates

router = APIRouter()


# ── Auth dependencies (redirect to /login when not authorised) ──
def require_login(request: Request):
    if not request.session.get("role"):
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return request.session


def require_admin(request: Request):
    if request.session.get("role") != "admin":
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return request.session


@router.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    username = username.strip()

    # Super admin
    if username == config.ADMIN_USERNAME and password == config.ADMIN_PASSWORD:
        request.session.clear()
        request.session["role"] = "admin"
        return RedirectResponse(url="/admin", status_code=303)

    # Business owner
    owner = get_owner_by_username(username)
    if owner and owner["active"] and check_password_hash(owner["password_hash"], password):
        request.session.clear()
        request.session["role"] = "owner"
        request.session["owner_id"] = owner["id"]
        return RedirectResponse(url="/bookings", status_code=303)

    return templates.TemplateResponse(
        request, "login.html", {"error": "Invalid username or password."})


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
