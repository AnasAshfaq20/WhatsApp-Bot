"""
WhatsApp Restaurant Bot
Stack: Flask + Meta WhatsApp Cloud API + Groq via LangChain
Features: Admin notifications, formatted bill, upselling
"""

import os
import json
import requests
from datetime import datetime
from flask import Flask, request, render_template, jsonify, session, redirect, url_for
from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from dotenv import load_dotenv

# utf-8-sig strips BOM if the IDE saved the file with one
load_dotenv(override=True, encoding="utf-8-sig")

# ---------- Setup ----------
app = Flask(__name__)

llm = ChatGroq(model="openai/gpt-oss-120b")

WHATSAPP_TOKEN    = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN      = os.getenv("WHATSAPP_VERIFY_TOKEN")
ADMIN_PHONE        = os.getenv("ADMIN_PHONE")
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "spicegarden2024")
app.secret_key     = os.getenv("SECRET_KEY", "fallback-secret-key")
META_API_URL       = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"

with open("menu.json", "r", encoding="utf-8") as f:
    MENU = json.load(f)

conversations = {}

# ---------- Upsell map ----------
# When customer orders these items, suggest these add-ons
UPSELL_MAP = {
    "Chicken Biryani":      ("a Raita or a cold drink", "Lassi"),
    "Beef Biryani":         ("a Raita or Garlic Naan", "Garlic Naan"),
    "Mutton Pulao":         ("a Roghni Naan or Lassi", "Lassi"),
    "Veg Biryani":          ("a cold drink", "Pepsi (500ml)"),
    "Chicken Karahi (Half)":("Naan or Garlic Naan", "Naan"),
    "Chicken Karahi (Full)":("Naan or Roghni Naan", "Naan"),
    "Mutton Karahi (Half)": ("Naan and a cold drink", "Naan"),
    "Seekh Kebab (4 pcs)":  ("Garlic Naan or Raita", "Garlic Naan"),
    "Chicken Tikka":        ("Naan and a Pepsi", "Naan"),
}

# ---------- System prompt ----------
def build_system_prompt():
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
5. Early in the conversation, ask for the customer's name if they haven't given it. Use it naturally in replies.
6. Always ask for delivery address before finalizing.
7. When the order is FINAL and confirmed by the customer, end your reply with this exact tag on its own line:
   [ORDER_CONFIRMED]
   Followed by a JSON block like:
   {{"name": "Ali Khan", "items": [{{"name": "...", "qty": 2, "price": 450}}], "total": 900, "address": "..."}}
8. If the user asks for something not on the menu, politely say it's unavailable and suggest similar items.
9. Currency is PKR (Pakistani Rupees) — show as "Rs. 450" format.
10. If the user sends rude, abusive, or offensive messages, respond with exactly: "Sorry, I can only assist with food orders. Please keep the conversation respectful."
11. If the user asks who you are, say: "I am the Spice Garden ordering assistant. I am here to help you place your food order."
12. If the user asks unrelated questions (politics, general knowledge, other restaurants), respond with: "I can only help with Spice Garden orders. Would you like to see our menu?"
13. Remember the customer's current order throughout the conversation — always show the running total when they add items.
14. If the customer says "show menu" or any variation, always show ALL categories with ALL items and prices in one message.
15. UPSELLING: When a customer adds a main dish (biryani, karahi, tikka, kebab), always suggest one relevant add-on item from the menu. Keep it to one short line. Example: "Would you like to add a Garlic Naan (Rs. 80) to go with that?"

Stay strictly focused on food ordering for Spice Garden only."""


# ---------- Helpers ----------
def get_history(phone_number):
    if phone_number not in conversations:
        conversations[phone_number] = {
            "messages": [SystemMessage(content=build_system_prompt())],
            "started_at": datetime.now().isoformat(),
        }
    return conversations[phone_number]["messages"]


def send_whatsapp(to, body):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    r = requests.post(META_API_URL, headers=headers, json=payload)
    if not r.ok:
        print(f"⚠️ Meta API error: {r.status_code} {r.text}")


def format_bill(order_data):
    """Generate a clean formatted bill to send to customer."""
    lines = []
    lines.append("ORDER CONFIRMED")
    lines.append("=" * 28)
    lines.append(f"Restaurant: {MENU['restaurant_name']}")
    lines.append(f"Customer: {order_data.get('name', 'Guest')}")
    lines.append(f"Date: {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    lines.append("-" * 28)
    for item in order_data["items"]:
        name  = item["name"]
        qty   = item["qty"]
        price = item["price"]
        subtotal = qty * price
        lines.append(f"{name} x{qty}")
        lines.append(f"  Rs. {price} x {qty} = Rs. {subtotal}")
    lines.append("-" * 28)
    lines.append(f"TOTAL: Rs. {order_data['total']}")
    lines.append("=" * 28)
    lines.append(f"Delivery to:")
    lines.append(order_data.get("address", "N/A"))
    lines.append("-" * 28)
    lines.append(f"Estimated delivery: 30-45 minutes")
    lines.append(f"Thank you for ordering from {MENU['restaurant_name']}!")
    return "\n".join(lines)


def notify_admin(order_data):
    """Send order alert to restaurant admin/owner via WhatsApp."""
    if not ADMIN_PHONE:
        print("⚠️ ADMIN_PHONE not set — skipping admin notification")
        return

    lines = []
    lines.append("NEW ORDER RECEIVED")
    lines.append("=" * 28)
    lines.append(f"Time: {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    lines.append(f"Customer: {order_data.get('name', 'Guest')} (+{order_data['phone']})")
    lines.append("-" * 28)
    for item in order_data["items"]:
        lines.append(f"  {item['name']} x{item['qty']} = Rs. {item['qty'] * item['price']}")
    lines.append("-" * 28)
    lines.append(f"TOTAL: Rs. {order_data['total']}")
    lines.append(f"Address: {order_data.get('address', 'N/A')}")
    lines.append("=" * 28)

    send_whatsapp(ADMIN_PHONE, "\n".join(lines))
    print(f"✅ Admin notified at {ADMIN_PHONE}")


def extract_and_log_order(reply_text, phone_number):
    if "[ORDER_CONFIRMED]" not in reply_text:
        return reply_text
    try:
        parts = reply_text.split("[ORDER_CONFIRMED]")
        clean_reply = parts[0].strip()
        json_part = parts[1].strip().replace("```json", "").replace("```", "").strip()

        order_data = json.loads(json_part)
        order_data["phone"] = phone_number
        order_data["timestamp"] = datetime.now().isoformat()
        order_data["status"] = "pending"

        try:
            with open("orders.json", "r", encoding="utf-8") as f:
                orders = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            orders = []

        orders.append(order_data)
        with open("orders.json", "w", encoding="utf-8") as f:
            json.dump(orders, f, indent=2, ensure_ascii=False)

        print(f"✅ ORDER LOGGED: {order_data}")

        # 1. Send formatted bill to customer
        bill = format_bill(order_data)
        send_whatsapp(phone_number, bill)

        # 2. Notify admin
        notify_admin(order_data)

        return clean_reply

    except Exception as e:
        print(f"⚠️ Order parse failed: {e}")
        return reply_text.split("[ORDER_CONFIRMED]")[0].strip()


# ---------- Webhook ----------
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
        messages = value.get("messages", [])

        if not messages:
            return "EVENT_RECEIVED", 200

        msg    = messages[0]
        sender = msg.get("from")
        m_type = msg.get("type", "")

        if m_type != "text":
            send_whatsapp(sender, "Sorry, I can only handle text messages right now.")
            return "EVENT_RECEIVED", 200

        incoming_msg = msg["text"]["body"].strip()
        print(f"Sender: {sender}")
        print(f"Message: {incoming_msg}")

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


@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "restaurant": MENU["restaurant_name"]}


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == DASHBOARD_USERNAME and password == DASHBOARD_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("view_orders"))
        return render_template("login.html", error="Invalid username or password.")
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/orders", methods=["GET"])
@login_required
def view_orders():
    return render_template("dashboard.html")


@app.route("/orders/data", methods=["GET"])
@login_required
def orders_data():
    try:
        with open("orders.json", "r", encoding="utf-8") as f:
            return jsonify({"orders": json.load(f)})
    except (FileNotFoundError, json.JSONDecodeError):
        return jsonify({"orders": []})


@app.route("/orders/update", methods=["POST"])
@login_required
def update_order_status():
    data = request.get_json()
    timestamp = data.get("timestamp")
    new_status = data.get("status")

    try:
        with open("orders.json", "r", encoding="utf-8") as f:
            orders = json.load(f)

        for order in orders:
            if order.get("timestamp") == timestamp:
                order["status"] = new_status
                phone = order["phone"]
                break

        with open("orders.json", "w", encoding="utf-8") as f:
            json.dump(orders, f, indent=2, ensure_ascii=False)

        # Notify customer — don't let this failure block the status update
        try:
            if new_status == "preparing":
                send_whatsapp(phone,
                    "Your order is now being prepared. Estimated delivery: 30-45 minutes.")
            elif new_status == "delivered":
                send_whatsapp(phone,
                    "Your order has been delivered. Enjoy your meal! Thank you for choosing Spice Garden.")
        except Exception as notify_err:
            print(f"⚠️ Notify failed: {notify_err}")

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/reset", methods=["GET"])
def reset_conversations():
    conversations.clear()
    return {"status": "conversations cleared"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
