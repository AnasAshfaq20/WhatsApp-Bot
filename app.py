"""
WhatsApp Restaurant Bot
Stack: Flask + Meta WhatsApp Cloud API + Groq via LangChain + PostgreSQL (Supabase)
"""

import os
import json
import psycopg2
import psycopg2.extras
import requests
from datetime import datetime
from functools import wraps
from flask import Flask, request, render_template, jsonify, session, redirect, url_for
from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from dotenv import load_dotenv

load_dotenv(override=True, encoding="utf-8-sig")

# ---------- Setup ----------
app = Flask(__name__)

llm = ChatGroq(model="openai/gpt-oss-120b")

WHATSAPP_TOKEN     = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID  = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN       = os.getenv("WHATSAPP_VERIFY_TOKEN")
ADMIN_PHONE        = os.getenv("ADMIN_PHONE")
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "spicegarden2024")
app.secret_key     = os.getenv("SECRET_KEY", "fallback-secret-key")
META_API_URL       = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
DATABASE_URL       = os.getenv("DATABASE_URL")

# In-memory state
conversations         = {}
processed_message_ids = set()


# ──────────────────────────────────────────────
# DATABASE  (PostgreSQL via Supabase)
# ──────────────────────────────────────────────
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    # Menu table
    c.execute("""
        CREATE TABLE IF NOT EXISTS menu (
            id       SERIAL PRIMARY KEY,
            category TEXT    NOT NULL,
            name     TEXT    NOT NULL,
            price    INTEGER NOT NULL,
            desc     TEXT    DEFAULT ''
        )
    """)

    # Restaurant info table
    c.execute("""
        CREATE TABLE IF NOT EXISTS restaurant_info (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Orders table
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id         SERIAL PRIMARY KEY,
            phone      TEXT    NOT NULL,
            name       TEXT    DEFAULT 'Guest',
            address    TEXT    NOT NULL,
            total      INTEGER NOT NULL,
            status     TEXT    DEFAULT 'pending',
            items_json TEXT    NOT NULL,
            created_at TEXT    NOT NULL
        )
    """)

    conn.commit()

    # Seed from menu.json if DB is empty
    c.execute("SELECT COUNT(*) FROM menu")
    count = c.fetchone()[0]
    if count == 0:
        _seed_from_json(c)
        conn.commit()
        print("Database seeded from menu.json")

    conn.close()


def _seed_from_json(cursor):
    with open("menu.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    # Restaurant info
    info = {
        "restaurant_name": data["restaurant_name"],
        "hours":           data["hours"],
        "location":        data["location"],
        "delivery_info":   data["delivery_info"],
    }
    for key, value in info.items():
        cursor.execute(
            "INSERT INTO restaurant_info (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value)
        )

    # Menu items
    for category, items in data["categories"].items():
        for item in items:
            cursor.execute(
                "INSERT INTO menu (category, name, price, desc) VALUES (%s, %s, %s, %s)",
                (category, item["name"], item["price"], item.get("desc", ""))
            )


def get_restaurant_info():
    conn = get_db()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT key, value FROM restaurant_info")
    rows = c.fetchall()
    conn.close()
    return {row["key"]: row["value"] for row in rows}


def get_menu_dict():
    """Return menu structured the same way as menu.json for the system prompt."""
    conn = get_db()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT category, name, price, desc FROM menu ORDER BY id")
    rows = c.fetchall()
    conn.close()
    info = get_restaurant_info()

    categories = {}
    for row in rows:
        cat = row["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append({
            "name":  row["name"],
            "price": row["price"],
            "desc":  row["desc"],
        })

    return {**info, "categories": categories}


def save_order(phone, name, address, total, items):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO orders (phone, name, address, total, status, items_json, created_at)
           VALUES (%s, %s, %s, %s, 'pending', %s, %s)""",
        (phone, name, address, total, json.dumps(items), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_all_orders():
    conn = get_db()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT * FROM orders ORDER BY id")
    rows = c.fetchall()
    conn.close()
    result = []
    for row in rows:
        d = dict(row)
        d["items"] = json.loads(d["items_json"])
        d["timestamp"] = d["created_at"]
        del d["items_json"]
        del d["created_at"]
        result.append(d)
    return result


def update_order_status_db(order_id, new_status):
    conn = get_db()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("UPDATE orders SET status = %s WHERE id = %s", (new_status, order_id))
    conn.commit()
    c.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
    row = c.fetchone()
    conn.close()
    if row:
        d = dict(row)
        d["items"] = json.loads(d["items_json"])
        return d
    return None


# ──────────────────────────────────────────────
# SYSTEM PROMPT
# ──────────────────────────────────────────────
def build_system_prompt():
    MENU     = get_menu_dict()
    menu_text = json.dumps(MENU, indent=2)
    return f"""You are a friendly WhatsApp ordering assistant for "{MENU['restaurant_name']}".

YOUR JOB:
- Greet customers warmly when they say hi
- Help them browse the menu
- Take their orders accurately
- Confirm orders with item names, quantities, and total price
- Answer questions about hours, location, delivery
- Upsell smartly when customer adds a main item

RESTAURANT INFO:
- Name: {MENU['restaurant_name']}
- Hours: {MENU['hours']}
- Location: {MENU['location']}
- Delivery: {MENU['delivery_info']}

MENU (use these exact prices):
{menu_text}

ORDERING RULES:
1. Keep replies SHORT — this is WhatsApp, not email. Use line breaks, not paragraphs.
2. Do not use any emojis in your replies.
3. Do NOT use any markdown formatting — no asterisks (*), no underscores (_), no hyphens for bullets, no bold, no italic. Plain text only.
4. When a customer wants to order, repeat back the order with prices and total.
5. Do NOT ask for the customer's name at the start. Only ask for their name and delivery address together when they are ready to confirm the order.
6. When the customer confirms their order, ask: "Please share your name and delivery address to complete the order."
7. When the order is FINAL and confirmed by the customer, end your reply with this exact tag on its own line:
   [ORDER_CONFIRMED]
   Followed by a JSON block like:
   {{"name": "Ali Khan", "items": [{{"name": "...", "qty": 2, "price": 450}}], "total": 900, "address": "..."}}
8. If the user asks for something not on the menu, politely say it is unavailable and suggest similar items.
9. Currency is PKR (Pakistani Rupees) — show as "Rs. 450" format.
10. If the user sends rude, abusive, or offensive messages, respond with exactly: "Sorry, I can only assist with food orders. Please keep the conversation respectful."
11. If the user asks who you are, say: "I am the Spice Garden ordering assistant. I am here to help you place your food order."
12. If the user asks unrelated questions (politics, general knowledge, other restaurants), respond with: "I can only help with Spice Garden orders. Would you like to see our menu?"
13. Remember the customer's current order throughout the conversation — always show the running total when they add items.
14. If the customer says "show menu" or any variation, always show ALL categories with ALL items and prices in one message.
15. UPSELLING: When a customer adds a main dish (biryani, karahi, tikka, kebab), always suggest one relevant add-on item from the menu. Keep it to one short line.

Stay strictly focused on food ordering for Spice Garden only."""


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def get_history(phone_number):
    if phone_number not in conversations:
        conversations[phone_number] = {
            "messages":   [SystemMessage(content=build_system_prompt())],
            "started_at": datetime.now().isoformat(),
        }
    return conversations[phone_number]["messages"]


def send_whatsapp(to, body):
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "text",
        "text": {"body": body},
    }
    r = requests.post(META_API_URL, headers=headers, json=payload)
    if not r.ok:
        print(f"⚠️ Meta API error: {r.status_code} {r.text}")


def format_bill(order_data):
    MENU  = get_restaurant_info()
    lines = [
        "ORDER CONFIRMED",
        "=" * 28,
        f"Restaurant: {MENU['restaurant_name']}",
        f"Customer: {order_data.get('name', 'Guest')}",
        f"Date: {datetime.now().strftime('%d %b %Y, %I:%M %p')}",
        "-" * 28,
    ]
    for item in order_data["items"]:
        lines.append(f"{item['name']} x{item['qty']}")
        lines.append(f"  Rs. {item['price']} x {item['qty']} = Rs. {item['qty'] * item['price']}")
    lines += [
        "-" * 28,
        f"TOTAL: Rs. {order_data['total']}",
        "=" * 28,
        "Delivery to:",
        order_data.get("address", "N/A"),
        "-" * 28,
        "Estimated delivery: 30-45 minutes",
        f"Thank you for ordering from {MENU['restaurant_name']}!",
    ]
    return "\n".join(lines)


def notify_admin(order_data):
    if not ADMIN_PHONE:
        return
    lines = [
        "NEW ORDER RECEIVED",
        "=" * 28,
        f"Time: {datetime.now().strftime('%d %b %Y, %I:%M %p')}",
        f"Customer: {order_data.get('name', 'Guest')} (+{order_data['phone']})",
        "-" * 28,
    ]
    for item in order_data["items"]:
        lines.append(f"  {item['name']} x{item['qty']} = Rs. {item['qty'] * item['price']}")
    lines += [
        "-" * 28,
        f"TOTAL: Rs. {order_data['total']}",
        f"Address: {order_data.get('address', 'N/A')}",
        "=" * 28,
    ]
    send_whatsapp(ADMIN_PHONE, "\n".join(lines))
    print(f"✅ Admin notified at {ADMIN_PHONE}")


def extract_and_log_order(reply_text, phone_number):
    if "[ORDER_CONFIRMED]" not in reply_text:
        return reply_text
    try:
        parts      = reply_text.split("[ORDER_CONFIRMED]")
        clean_reply = parts[0].strip()
        json_part  = parts[1].strip().replace("```json", "").replace("```", "").strip()

        order_data = json.loads(json_part)
        order_data["phone"] = phone_number

        # Save to SQLite
        save_order(
            phone   = phone_number,
            name    = order_data.get("name", "Guest"),
            address = order_data.get("address", ""),
            total   = order_data.get("total", 0),
            items   = order_data.get("items", []),
        )
        print(f"✅ ORDER SAVED TO DB: {order_data}")

        # Send bill to customer
        send_whatsapp(phone_number, format_bill(order_data))

        # Notify admin
        try:
            notify_admin(order_data)
        except Exception as e:
            print(f"⚠️ Admin notify failed: {e}")

        return clean_reply

    except Exception as e:
        print(f"⚠️ Order parse failed: {e}")
        return reply_text.split("[ORDER_CONFIRMED]")[0].strip()


# ──────────────────────────────────────────────
# AUTH
# ──────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if (request.form.get("username") == DASHBOARD_USERNAME and
                request.form.get("password") == DASHBOARD_PASSWORD):
            session["logged_in"] = True
            return redirect(url_for("view_orders"))
        return render_template("login.html", error="Invalid username or password.")
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ──────────────────────────────────────────────
# WEBHOOK
# ──────────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("Webhook verified successfully")
        return challenge or "", 200
    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json(silent=True) or {}
    try:
        entry    = data.get("entry", [])[0]
        changes  = entry.get("changes", [])[0]
        value    = changes.get("value", {})

        # Skip delivery/read receipts
        if value.get("statuses"):
            return "EVENT_RECEIVED", 200

        messages = value.get("messages", [])
        if not messages:
            return "EVENT_RECEIVED", 200

        msg        = messages[0]
        sender     = msg.get("from")
        m_type     = msg.get("type", "")
        message_id = msg.get("id", "")

        # Deduplicate — Meta sometimes retries webhooks
        if message_id and message_id in processed_message_ids:
            print(f"⚠️ Duplicate ignored: {message_id}")
            return "EVENT_RECEIVED", 200
        if message_id:
            processed_message_ids.add(message_id)
            if len(processed_message_ids) > 1000:
                processed_message_ids.pop()

        if m_type != "text":
            send_whatsapp(sender, "Sorry, I can only handle text messages right now.")
            return "EVENT_RECEIVED", 200

        incoming_msg = msg["text"]["body"].strip()
        print(f"Sender: {sender} | MsgID: {message_id} | Msg: {incoming_msg}")

        history = get_history(sender)
        history.append(HumanMessage(content=incoming_msg))

        response = llm.invoke(history)
        reply    = response.content
        history.append(AIMessage(content=reply))

        reply = extract_and_log_order(reply, sender)
        print(f"Reply: {reply}")
        send_whatsapp(sender, reply)

    except Exception as e:
        print("Error processing webhook:", e)

    return "EVENT_RECEIVED", 200


# ──────────────────────────────────────────────
# DASHBOARD ROUTES
# ──────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    info = get_restaurant_info()
    return {"status": "ok", "restaurant": info.get("restaurant_name", "Unknown")}


@app.route("/orders", methods=["GET"])
@login_required
def view_orders():
    return render_template("dashboard.html")


@app.route("/orders/data", methods=["GET"])
@login_required
def orders_data():
    return jsonify({"orders": get_all_orders()})


@app.route("/orders/update", methods=["POST"])
@login_required
def update_order_status():
    data       = request.get_json()
    order_id   = data.get("id")
    new_status = data.get("status")

    try:
        order = update_order_status_db(order_id, new_status)
        if not order:
            return jsonify({"success": False, "error": "Order not found"})

        # Notify customer
        try:
            if new_status == "preparing":
                send_whatsapp(order["phone"],
                    "Your order is now being prepared. Estimated delivery: 30-45 minutes.")
            elif new_status == "delivered":
                send_whatsapp(order["phone"],
                    "Your order has been delivered. Enjoy your meal! Thank you for choosing Spice Garden.")
        except Exception as e:
            print(f"⚠️ Notify failed: {e}")

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/reset", methods=["GET"])
def reset_conversations():
    conversations.clear()
    return {"status": "conversations cleared"}


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
