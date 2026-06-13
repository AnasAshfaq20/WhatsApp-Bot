import os
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

load_dotenv(override=True, encoding="utf-8-sig")

# Pakistan Standard Time (UTC+5) — Render runs in UTC
PKT = timezone(timedelta(hours=5))


def now_pkt():
    """Current time in Pakistan, regardless of server timezone."""
    return datetime.now(PKT)


def now_utc():
    """Current time as an absolute UTC instant.

    Stored timestamps use this so the dashboard can render each order in
    the viewer's own local timezone, wherever they are.
    """
    return datetime.now(timezone.utc)

# Super admin (manages owners)
ADMIN_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "spicegarden2024")

SECRET_KEY   = os.getenv("SECRET_KEY", "fallback-secret-key")
DATABASE_URL = os.getenv("DATABASE_URL")

# Shared secret the voice platform (Vapi) must send to call /voice/order
VOICE_WEBHOOK_SECRET = os.getenv("VOICE_WEBHOOK_SECRET", "change-this-voice-secret")

# Default tenant credentials (used to seed the first owner)
WHATSAPP_TOKEN    = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN      = os.getenv("WHATSAPP_VERIFY_TOKEN")
ADMIN_PHONE       = os.getenv("ADMIN_PHONE")

GRAPH_API_BASE = "https://graph.facebook.com/v19.0"

DEFAULT_MENU_IMAGE_URL = (
    "https://raw.githubusercontent.com/AnasAshfaq20/WhatsApp-Bot/main/spice_garden_menu.png"
)
