import json
from datetime import datetime

import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash

from . import config
from .config import now_utc


def get_db():
    return psycopg2.connect(config.DATABASE_URL)


def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ──────────────────────────────────────────────
# SCHEMA
# ──────────────────────────────────────────────
def _rename_column_if_exists(cursor, table, old, new):
    """Migrate a column rename only when the old name is still present."""
    cursor.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
    """, (table, old))
    if cursor.fetchone():
        cursor.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS owners (
            id                SERIAL PRIMARY KEY,
            username          TEXT    UNIQUE NOT NULL,
            password_hash     TEXT    NOT NULL,
            owner_name        TEXT    NOT NULL,
            business_name     TEXT    NOT NULL,
            hours             TEXT    DEFAULT '',
            location          TEXT    DEFAULT '',
            service_area      TEXT    DEFAULT '',
            whatsapp_token    TEXT    DEFAULT '',
            whatsapp_phone_id TEXT    DEFAULT '',
            admin_phone       TEXT    DEFAULT '',
            fleet_image_url   TEXT    DEFAULT '',
            active            BOOLEAN DEFAULT TRUE,
            created_at        TEXT    NOT NULL
        )
    """)

    # Migrations from the restaurant-era schema (no-ops on a fresh DB)
    _rename_column_if_exists(c, "owners", "restaurant_name", "business_name")
    _rename_column_if_exists(c, "owners", "delivery_info", "service_area")
    _rename_column_if_exists(c, "owners", "menu_image_url", "fleet_image_url")
    _rename_column_if_exists(c, "owners", "menu_image", "fleet_image")
    _rename_column_if_exists(c, "owners", "menu_image_mime", "fleet_image_mime")

    # Uploaded fleet brochure image, stored in the DB and served from /fleet-image/<id>
    c.execute("ALTER TABLE owners ADD COLUMN IF NOT EXISTS fleet_image BYTEA")
    c.execute("ALTER TABLE owners ADD COLUMN IF NOT EXISTS fleet_image_mime TEXT DEFAULT ''")

    # Voice agent phone number (the number customers call) — routes calls to this owner
    c.execute("ALTER TABLE owners ADD COLUMN IF NOT EXISTS voice_phone TEXT DEFAULT ''")

    # Currency symbol shown to customers (e.g. $, Rs., AED)
    c.execute("ALTER TABLE owners ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT '$'")

    # Facebook Messenger + Instagram DM channels (same Meta app, per-owner page)
    c.execute("ALTER TABLE owners ADD COLUMN IF NOT EXISTS fb_page_id TEXT DEFAULT ''")
    c.execute("ALTER TABLE owners ADD COLUMN IF NOT EXISTS fb_page_token TEXT DEFAULT ''")
    c.execute("ALTER TABLE owners ADD COLUMN IF NOT EXISTS ig_account_id TEXT DEFAULT ''")
    # "Instagram Login" accounts have their own token + app-scoped user id
    c.execute("ALTER TABLE owners ADD COLUMN IF NOT EXISTS ig_token TEXT DEFAULT ''")
    c.execute("ALTER TABLE owners ADD COLUMN IF NOT EXISTS ig_app_id TEXT DEFAULT ''")

    c.execute("""
        CREATE TABLE IF NOT EXISTS vehicles (
            id           SERIAL PRIMARY KEY,
            owner_id     INTEGER,
            category     TEXT    NOT NULL,
            name         TEXT    NOT NULL,
            capacity     INTEGER NOT NULL DEFAULT 4,
            hourly_rate  INTEGER NOT NULL,
            min_hours    INTEGER NOT NULL DEFAULT 2,
            airport_rate INTEGER NOT NULL DEFAULT 0,
            description  TEXT    DEFAULT '',
            available    BOOLEAN DEFAULT TRUE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id               SERIAL PRIMARY KEY,
            owner_id         INTEGER,
            phone            TEXT    NOT NULL,
            name             TEXT    DEFAULT 'Guest',
            vehicle          TEXT    NOT NULL,
            booking_type     TEXT    DEFAULT 'hourly',
            pickup_location  TEXT    NOT NULL,
            dropoff_location TEXT    DEFAULT '',
            pickup_time      TEXT    NOT NULL,
            hours            INTEGER DEFAULT 0,
            passengers       INTEGER DEFAULT 1,
            occasion         TEXT    DEFAULT '',
            total            INTEGER NOT NULL,
            status           TEXT    DEFAULT 'pending',
            driver_name      TEXT    DEFAULT '',
            driver_phone     TEXT    DEFAULT '',
            created_at       TEXT    NOT NULL
        )
    """)
    # Where the booking came from: whatsapp, facebook, instagram, voice
    c.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS channel TEXT DEFAULT 'whatsapp'")

    conn.commit()

    default_owner_id = _ensure_default_owner(conn)

    # Seed default owner's fleet from fleet.json if empty
    c.execute("SELECT COUNT(*) FROM vehicles WHERE owner_id = %s", (default_owner_id,))
    if c.fetchone()[0] == 0:
        _seed_fleet_from_json(c, default_owner_id)
        conn.commit()
        print("Default owner fleet seeded from fleet.json")

    conn.close()


def _ensure_default_owner(conn):
    """Create the demo limo company from env credentials on first run."""
    c = conn.cursor()
    c.execute("SELECT id FROM owners WHERE username = %s", ("luxride",))
    row = c.fetchone()
    if row:
        return row[0]

    with open("fleet.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    c.execute(
        """INSERT INTO owners (username, password_hash, owner_name, business_name,
                               hours, location, service_area, currency,
                               whatsapp_token, whatsapp_phone_id, admin_phone,
                               fleet_image_url, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (
            "luxride",
            generate_password_hash(config.ADMIN_PASSWORD),
            "LuxRide Owner",
            data["business_name"],
            data["hours"],
            data["location"],
            data["service_area"],
            data.get("currency", "$"),
            config.WHATSAPP_TOKEN or "",
            config.WHATSAPP_PHONE_ID or "",
            config.ADMIN_PHONE or "",
            "",
            datetime.now().isoformat(),
        ),
    )
    owner_id = c.fetchone()[0]
    conn.commit()
    print(f"Default owner 'luxride' created (id={owner_id})")
    return owner_id


def _seed_fleet_from_json(cursor, owner_id):
    with open("fleet.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    for category, vehicles in data["fleet"].items():
        for v in vehicles:
            cursor.execute(
                """INSERT INTO vehicles (owner_id, category, name, capacity, hourly_rate,
                                         min_hours, airport_rate, description)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (owner_id, category, v["name"], v["capacity"], v["hourly_rate"],
                 v.get("min_hours", 2), v.get("airport_rate", 0), v.get("desc", "")),
            )


# ──────────────────────────────────────────────
# OWNERS
# ──────────────────────────────────────────────
def create_owner(username, password, owner_name, business_name, hours, location,
                 service_area, whatsapp_token, whatsapp_phone_id, admin_phone,
                 fleet_image_url, voice_phone="", currency="$",
                 fb_page_id="", fb_page_token="", ig_account_id=""):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO owners (username, password_hash, owner_name, business_name,
                               hours, location, service_area, currency,
                               whatsapp_token, whatsapp_phone_id, admin_phone,
                               fleet_image_url, voice_phone,
                               fb_page_id, fb_page_token, ig_account_id, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (username, generate_password_hash(password), owner_name, business_name,
         hours, location, service_area, currency, whatsapp_token, whatsapp_phone_id,
         admin_phone, fleet_image_url, voice_phone,
         fb_page_id, fb_page_token, ig_account_id, datetime.now().isoformat()),
    )
    owner_id = c.fetchone()[0]
    conn.commit()
    conn.close()
    return owner_id


def update_owner(owner_id, fields):
    """Update allowed owner fields. fields: dict of column -> value."""
    allowed = {"owner_name", "business_name", "hours", "location", "service_area",
               "whatsapp_token", "whatsapp_phone_id", "admin_phone", "currency",
               "fleet_image_url", "voice_phone", "active", "username",
               "fb_page_id", "fb_page_token", "ig_account_id", "ig_token", "ig_app_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "password" in fields and fields["password"]:
        updates["password_hash"] = generate_password_hash(fields["password"])
    if not updates:
        return

    set_clause = ", ".join(f"{col} = %s" for col in updates)
    conn = get_db()
    c = conn.cursor()
    c.execute(f"UPDATE owners SET {set_clause} WHERE id = %s",
              (*updates.values(), owner_id))
    conn.commit()
    conn.close()


def delete_owner(owner_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM bookings WHERE owner_id = %s", (owner_id,))
    c.execute("DELETE FROM vehicles WHERE owner_id = %s", (owner_id,))
    c.execute("DELETE FROM owners   WHERE id = %s", (owner_id,))
    conn.commit()
    conn.close()


# Owner columns without the raw image bytes (too heavy for regular queries)
OWNER_COLS = ("id, username, password_hash, owner_name, business_name, hours, "
              "location, service_area, currency, whatsapp_token, whatsapp_phone_id, "
              "admin_phone, fleet_image_url, voice_phone, active, created_at, "
              "fb_page_id, fb_page_token, ig_account_id, ig_token, ig_app_id")


def get_owner_by_id(owner_id):
    conn = get_db()
    c = dict_cursor(conn)
    c.execute(f"SELECT {OWNER_COLS} FROM owners WHERE id = %s", (owner_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_owner_by_username(username):
    conn = get_db()
    c = dict_cursor(conn)
    c.execute(f"SELECT {OWNER_COLS} FROM owners WHERE username = %s", (username,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_owner_by_phone_id(whatsapp_phone_id):
    conn = get_db()
    c = dict_cursor(conn)
    # Newest active owner wins if the same phone ID was reused across tenants
    c.execute(f"""SELECT {OWNER_COLS} FROM owners
                  WHERE whatsapp_phone_id = %s AND active = TRUE
                  ORDER BY id DESC LIMIT 1""",
              (whatsapp_phone_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_owner_by_fb_page(fb_page_id):
    conn = get_db()
    c = dict_cursor(conn)
    c.execute(f"""SELECT {OWNER_COLS} FROM owners
                  WHERE fb_page_id = %s AND active = TRUE
                  ORDER BY id DESC LIMIT 1""", (str(fb_page_id),))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_owner_by_ig_account(ig_account_id):
    """Webhook entry.id may be the IG professional id or the app-scoped id
    depending on how the account was connected — match either."""
    conn = get_db()
    c = dict_cursor(conn)
    c.execute(f"""SELECT {OWNER_COLS} FROM owners
                  WHERE (ig_account_id = %s OR ig_app_id = %s) AND active = TRUE
                  ORDER BY id DESC LIMIT 1""",
              (str(ig_account_id), str(ig_account_id)))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_owner_by_voice_phone(voice_phone):
    """Match the number the customer called. Tolerates +/spaces differences."""
    digits = "".join(ch for ch in (voice_phone or "") if ch.isdigit())
    if not digits:
        return None
    conn = get_db()
    c = dict_cursor(conn)
    # Compare on digits only so "+1 555..." and "1555..." both match
    c.execute(f"""SELECT {OWNER_COLS} FROM owners
                  WHERE active = TRUE
                    AND regexp_replace(voice_phone, '[^0-9]', '', 'g') = %s""",
              (digits,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_owners():
    conn = get_db()
    c = dict_cursor(conn)
    c.execute(f"""
        SELECT {', '.join('o.' + col.strip() for col in OWNER_COLS.split(','))},
               COUNT(b.id) AS booking_count,
               COALESCE(SUM(b.total), 0) AS revenue
        FROM owners o
        LEFT JOIN bookings b ON b.owner_id = o.id
        GROUP BY o.id
        ORDER BY o.id
    """)
    rows = c.fetchall()
    conn.close()
    result = []
    for row in rows:
        d = dict(row)
        d.pop("password_hash", None)
        result.append(d)
    return result


def save_fleet_image(owner_id, data, mime, public_url):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "UPDATE owners SET fleet_image = %s, fleet_image_mime = %s, fleet_image_url = %s WHERE id = %s",
        (psycopg2.Binary(data), mime, public_url, owner_id),
    )
    conn.commit()
    conn.close()


def get_fleet_image(owner_id):
    """Returns (bytes, mime) or None."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT fleet_image, fleet_image_mime FROM owners WHERE id = %s", (owner_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        return bytes(row[0]), row[1] or "image/png"
    return None


# ──────────────────────────────────────────────
# VEHICLES (FLEET)
# ──────────────────────────────────────────────
def get_vehicles_for_owner(owner_id, only_available=False):
    conn = get_db()
    c = dict_cursor(conn)
    query = "SELECT * FROM vehicles WHERE owner_id = %s"
    if only_available:
        query += " AND available = TRUE"
    c.execute(query + " ORDER BY id", (owner_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def add_vehicle(owner_id, category, name, capacity, hourly_rate, min_hours,
                airport_rate, description=""):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO vehicles (owner_id, category, name, capacity, hourly_rate,
                                 min_hours, airport_rate, description)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (owner_id, category, name, capacity, hourly_rate, min_hours,
         airport_rate, description),
    )
    vehicle_id = c.fetchone()[0]
    conn.commit()
    conn.close()
    return vehicle_id


def update_vehicle(vehicle_id, owner_id, fields):
    """Update a vehicle, scoped to its owner so tenants can't touch others' fleets."""
    allowed = {"category", "name", "capacity", "hourly_rate", "min_hours",
               "airport_rate", "description", "available"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{col} = %s" for col in updates)
    conn = get_db()
    c = conn.cursor()
    c.execute(f"UPDATE vehicles SET {set_clause} WHERE id = %s AND owner_id = %s",
              (*updates.values(), vehicle_id, owner_id))
    conn.commit()
    conn.close()


def delete_vehicle(vehicle_id, owner_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM vehicles WHERE id = %s AND owner_id = %s",
              (vehicle_id, owner_id))
    conn.commit()
    conn.close()


def get_fleet_dict(owner):
    """Fleet + company info structured for the system prompt."""
    vehicles = get_vehicles_for_owner(owner["id"], only_available=True)

    categories = {}
    for v in vehicles:
        categories.setdefault(v["category"], []).append({
            "name":         v["name"],
            "capacity":     v["capacity"],
            "hourly_rate":  v["hourly_rate"],
            "min_hours":    v["min_hours"],
            "airport_rate": v["airport_rate"] or None,
            "desc":         v["description"],
        })

    return {
        "business_name": owner["business_name"],
        "hours":         owner["hours"],
        "location":      owner["location"],
        "service_area":  owner["service_area"],
        "currency":      owner.get("currency") or "$",
        "fleet":         categories,
    }


# ──────────────────────────────────────────────
# BOOKINGS
# ──────────────────────────────────────────────
def save_booking(owner_id, phone, name, vehicle, booking_type, pickup_location,
                 dropoff_location, pickup_time, hours, passengers, occasion, total,
                 channel="whatsapp"):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO bookings (owner_id, phone, name, vehicle, booking_type,
                                 pickup_location, dropoff_location, pickup_time,
                                 hours, passengers, occasion, total, status, channel,
                                 created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
           RETURNING id""",
        (owner_id, phone, name, vehicle, booking_type, pickup_location,
         dropoff_location, pickup_time, hours, passengers, occasion, total,
         channel, now_utc().isoformat()),
    )
    booking_id = c.fetchone()[0]
    conn.commit()
    conn.close()
    return booking_id


def get_bookings_for_owner(owner_id):
    conn = get_db()
    c = dict_cursor(conn)
    c.execute("SELECT * FROM bookings WHERE owner_id = %s ORDER BY id", (owner_id,))
    rows = c.fetchall()
    conn.close()
    return [_booking_row_to_dict(row) for row in rows]


def update_booking_status_db(booking_id, new_status, owner_id=None,
                             driver_name=None, driver_phone=None):
    """Update status (and optionally the assigned driver). If owner_id is given,
    only that owner's booking can be updated."""
    sets = ["status = %s"]
    values = [new_status]
    if driver_name is not None:
        sets.append("driver_name = %s")
        values.append(driver_name)
    if driver_phone is not None:
        sets.append("driver_phone = %s")
        values.append(driver_phone)

    conn = get_db()
    c = dict_cursor(conn)
    if owner_id is not None:
        c.execute(f"UPDATE bookings SET {', '.join(sets)} WHERE id = %s AND owner_id = %s",
                  (*values, booking_id, owner_id))
    else:
        c.execute(f"UPDATE bookings SET {', '.join(sets)} WHERE id = %s",
                  (*values, booking_id))
    if c.rowcount == 0:
        # Nothing updated — booking doesn't exist or belongs to another owner
        conn.close()
        return None
    conn.commit()
    c.execute("SELECT * FROM bookings WHERE id = %s", (booking_id,))
    row = c.fetchone()
    conn.close()
    return _booking_row_to_dict(row) if row else None


def _booking_row_to_dict(row):
    d = dict(row)
    d["ref"] = booking_ref(d["id"])
    d["timestamp"] = d["created_at"]
    del d["created_at"]
    return d


def booking_ref(booking_id):
    """Human-friendly booking reference, e.g. LX-0042."""
    return f"LX-{booking_id:04d}"
