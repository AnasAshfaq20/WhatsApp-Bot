from functools import wraps

from flask import Blueprint, request, render_template, session, redirect, url_for
from werkzeug.security import check_password_hash

from .. import config
from ..db import get_owner_by_username

auth_bp = Blueprint("auth", __name__)


def login_required(f):
    """Any logged-in user (admin or owner)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("role"):
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "admin":
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # Super admin
        if username == config.ADMIN_USERNAME and password == config.ADMIN_PASSWORD:
            session.clear()
            session["role"] = "admin"
            return redirect(url_for("admin.admin_panel"))

        # Restaurant owner
        owner = get_owner_by_username(username)
        if owner and owner["active"] and check_password_hash(owner["password_hash"], password):
            session.clear()
            session["role"]     = "owner"
            session["owner_id"] = owner["id"]
            return redirect(url_for("dashboard.view_orders"))

        return render_template("login.html", error="Invalid username or password.")
    return render_template("login.html", error=None)


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
