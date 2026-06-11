import json
from datetime import datetime

import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash

from . import config


def get_db():
    return psycopg2.connect(config.DATABASE_URL)


def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ──────────────────────────────────────────────
# SCHEMA
# ──────────────────────────────────────────────
def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS owners (
            id                SERIAL PRIMARY KEY,
            username          TEXT    UNIQUE NOT NULL,
            password_hash     TEXT    NOT NULL,
            owner_name        TEXT    NOT NULL,
            restaurant_name   TEXT    NOT NULL,
            hours             TEXT    DEFAULT '',
            location          TEXT    DEFAULT '',
            delivery_info     TEXT    DEFAULT '',
            whatsapp_token    TEXT    DEFAULT '',
            whatsapp_phone_id TEXT    DEFAULT '',
            admin_phone       TEXT    DEFAULT '',
            menu_image_url    TEXT    DEFAULT '',
            active            BOOLEAN DEFAULT TRUE,
            created_at        TEXT    NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS menu (
            id          SERIAL PRIMARY KEY,
            owner_id    INTEGER,
            category    TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            price       INTEGER NOT NULL,
            description TEXT    DEFAULT ''
        )
    """)
    c.execute("ALTER TABLE menu ADD COLUMN IF NOT EXISTS owner_id INTEGER")

    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id         SERIAL PRIMARY KEY,
            owner_id   INTEGER,
            phone      TEXT    NOT NULL,
            name       TEXT    DEFAULT 'Guest',
            address    TEXT    NOT NULL,
            total      INTEGER NOT NULL,
            status     TEXT    DEFAULT 'pending',
            items_json TEXT    NOT NULL,
            created_at TEXT    NOT NULL
        )
    """)
    c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS owner_id INTEGER")

    conn.commit()

    default_owner_id = _ensure_default_owner(conn)

    # Attach any pre-multi-tenant rows to the default owner
    c.execute("UPDATE menu   SET owner_id = %s WHERE owner_id IS NULL", (default_owner_id,))
    c.execute("UPDATE orders SET owner_id = %s WHERE owner_id IS NULL", (default_owner_id,))
    conn.commit()

    # Seed default owner's menu from menu.json if empty
    c.execute("SELECT COUNT(*) FROM menu WHERE owner_id = %s", (default_owner_id,))
    if c.fetchone()[0] == 0:
        _seed_menu_from_json(c, default_owner_id)
        conn.commit()
        print("Default owner menu seeded from menu.json")

    conn.close()


def _ensure_default_owner(conn):
    """Create the Spice Garden owner from env credentials on first run."""
    c = conn.cursor()
    c.execute("SELECT id FROM owners WHERE username = %s", ("spicegarden",))
    row = c.fetchone()
    if row:
        return row[0]

    with open("menu.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    c.execute(
        """INSERT INTO owners (username, password_hash, owner_name, restaurant_name,
                               hours, location, delivery_info,
                               whatsapp_token, whatsapp_phone_id, admin_phone,
                               menu_image_url, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (
            "spicegarden",
            generate_password_hash(config.ADMIN_PASSWORD),
            "Spice Garden Owner",
            data["restaurant_name"],
            data["hours"],
            data["location"],
            data["delivery_info"],
            config.WHATSAPP_TOKEN or "",
            config.WHATSAPP_PHONE_ID or "",
            config.ADMIN_PHONE or "",
            config.DEFAULT_MENU_IMAGE_URL,
            datetime.now().isoformat(),
        ),
    )
    owner_id = c.fetchone()[0]
    conn.commit()
    print(f"Default owner 'spicegarden' created (id={owner_id})")
    return owner_id


def _seed_menu_from_json(cursor, owner_id):
    with open("menu.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    for category, items in data["categories"].items():
        for item in items:
            cursor.execute(
                "INSERT INTO menu (owner_id, category, name, price, description) VALUES (%s, %s, %s, %s, %s)",
                (owner_id, category, item["name"], item["price"], item.get("desc", "")),
            )


# ──────────────────────────────────────────────
# OWNERS
# ──────────────────────────────────────────────
def create_owner(username, password, owner_name, restaurant_name, hours, location,
                 delivery_info, whatsapp_token, whatsapp_phone_id, admin_phone,
                 menu_image_url):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO owners (username, password_hash, owner_name, restaurant_name,
                               hours, location, delivery_info,
                               whatsapp_token, whatsapp_phone_id, admin_phone,
                               menu_image_url, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (username, generate_password_hash(password), owner_name, restaurant_name,
         hours, location, delivery_info, whatsapp_token, whatsapp_phone_id,
         admin_phone, menu_image_url, datetime.now().isoformat()),
    )
    owner_id = c.fetchone()[0]
    conn.commit()
    conn.close()
    return owner_id


def update_owner(owner_id, fields):
    """Update allowed owner fields. fields: dict of column -> value."""
    allowed = {"owner_name", "restaurant_name", "hours", "location", "delivery_info",
               "whatsapp_token", "whatsapp_phone_id", "admin_phone",
               "menu_image_url", "active", "username"}
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
    c.execute("DELETE FROM orders WHERE owner_id = %s", (owner_id,))
    c.execute("DELETE FROM menu   WHERE owner_id = %s", (owner_id,))
    c.execute("DELETE FROM owners WHERE id = %s", (owner_id,))
    conn.commit()
    conn.close()


def get_owner_by_id(owner_id):
    conn = get_db()
    c = dict_cursor(conn)
    c.execute("SELECT * FROM owners WHERE id = %s", (owner_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_owner_by_username(username):
    conn = get_db()
    c = dict_cursor(conn)
    c.execute("SELECT * FROM owners WHERE username = %s", (username,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_owner_by_phone_id(whatsapp_phone_id):
    conn = get_db()
    c = dict_cursor(conn)
    c.execute("SELECT * FROM owners WHERE whatsapp_phone_id = %s AND active = TRUE",
              (whatsapp_phone_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_owners():
    conn = get_db()
    c = dict_cursor(conn)
    c.execute("""
        SELECT o.*, COUNT(ord.id) AS order_count,
               COALESCE(SUM(ord.total), 0) AS revenue
        FROM owners o
        LEFT JOIN orders ord ON ord.owner_id = o.id
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


# ──────────────────────────────────────────────
# MENU
# ──────────────────────────────────────────────
def get_menu_dict(owner):
    """Menu + restaurant info structured for the system prompt."""
    conn = get_db()
    c = dict_cursor(conn)
    c.execute(
        "SELECT category, name, price, description FROM menu WHERE owner_id = %s ORDER BY id",
        (owner["id"],),
    )
    rows = c.fetchall()
    conn.close()

    categories = {}
    for row in rows:
        categories.setdefault(row["category"], []).append({
            "name":  row["name"],
            "price": row["price"],
            "desc":  row["description"],
        })

    return {
        "restaurant_name": owner["restaurant_name"],
        "hours":           owner["hours"],
        "location":        owner["location"],
        "delivery_info":   owner["delivery_info"],
        "categories":      categories,
    }


# ──────────────────────────────────────────────
# ORDERS
# ──────────────────────────────────────────────
def save_order(owner_id, phone, name, address, total, items):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO orders (owner_id, phone, name, address, total, status, items_json, created_at)
           VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s)""",
        (owner_id, phone, name, address, total, json.dumps(items),
         datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_orders_for_owner(owner_id):
    conn = get_db()
    c = dict_cursor(conn)
    c.execute("SELECT * FROM orders WHERE owner_id = %s ORDER BY id", (owner_id,))
    rows = c.fetchall()
    conn.close()
    return [_order_row_to_dict(row) for row in rows]


def update_order_status_db(order_id, new_status, owner_id=None):
    """Update status. If owner_id given, only that owner's order can be updated."""
    conn = get_db()
    c = dict_cursor(conn)
    if owner_id is not None:
        c.execute("UPDATE orders SET status = %s WHERE id = %s AND owner_id = %s",
                  (new_status, order_id, owner_id))
    else:
        c.execute("UPDATE orders SET status = %s WHERE id = %s", (new_status, order_id))
    if c.rowcount == 0:
        # Nothing updated — order doesn't exist or belongs to another owner
        conn.close()
        return None
    conn.commit()
    c.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
    row = c.fetchone()
    conn.close()
    return _order_row_to_dict(row) if row else None


def _order_row_to_dict(row):
    d = dict(row)
    d["items"] = json.loads(d["items_json"])
    d["timestamp"] = d["created_at"]
    del d["items_json"]
    del d["created_at"]
    return d
