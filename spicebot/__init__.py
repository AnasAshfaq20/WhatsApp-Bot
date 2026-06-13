"""
WhatsApp Restaurant Bot — multi-tenant.
Stack: Flask + Meta WhatsApp Cloud API + Groq via LangChain + PostgreSQL (Supabase)
"""
from flask import Flask

from . import config
from .db import init_db


def create_app():
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = config.SECRET_KEY

    from .routes.auth import auth_bp
    from .routes.webhook import webhook_bp
    from .routes.dashboard import dashboard_bp
    from .routes.admin import admin_bp
    from .routes.voice import voice_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(voice_bp)

    init_db()
    return app
