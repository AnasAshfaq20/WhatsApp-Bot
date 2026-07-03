"""
WhatsApp + voice limo & car rental booking bot — multi-tenant.
Stack: FastAPI + Meta WhatsApp Cloud API + Groq via LangChain + PostgreSQL (Supabase)
"""
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .db import init_db


def create_app():
    app = FastAPI(title="LuxRide Booking Bot")
    app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY)

    from .routes.auth import router as auth_router
    from .routes.webhook import router as webhook_router
    from .routes.dashboard import router as dashboard_router
    from .routes.admin import router as admin_router
    from .routes.voice import router as voice_router

    app.include_router(auth_router)
    app.include_router(webhook_router)
    app.include_router(dashboard_router)
    app.include_router(admin_router)
    app.include_router(voice_router)

    init_db()
    return app
