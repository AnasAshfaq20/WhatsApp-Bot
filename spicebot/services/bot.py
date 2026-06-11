"""Conversation engine: system prompt, LLM, order extraction, notifications."""
import json
from datetime import datetime

from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from ..db import get_menu_dict, save_order
from . import whatsapp

llm = ChatGroq(model="openai/gpt-oss-120b")

# In-memory conversation state, keyed by (owner_id, customer_phone)
conversations = {}


def build_system_prompt(owner):
    menu      = get_menu_dict(owner)
    menu_text = json.dumps(menu, indent=2)
    name      = menu["restaurant_name"]
    return f"""You are a friendly WhatsApp ordering assistant for "{name}".

YOUR JOB:
- Greet customers warmly when they say hi
- Help them browse the menu
- Take their orders accurately
- Confirm orders with item names, quantities, and total price
- Answer questions about hours, location, delivery
- Upsell smartly when customer adds a main item

RESTAURANT INFO:
- Name: {name}
- Hours: {menu['hours']}
- Location: {menu['location']}
- Delivery: {menu['delivery_info']}

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
11. If the user asks who you are, say: "I am the {name} ordering assistant. I am here to help you place your food order."
12. If the user asks unrelated questions (politics, general knowledge, other restaurants), respond with: "I can only help with {name} orders. Would you like to see our menu?"
13. Remember the customer's current order throughout the conversation — always show the running total when they add items.
14. Whenever you want to show the menu to the customer (they ask for it, say yes to seeing it, or ask what food is available), output the tag [SEND_MENU] on its own line. Do NOT list menu items in text — the image will be sent automatically.
15. UPSELLING: When a customer adds a main dish, suggest one relevant add-on from the menu. Keep it to one short line. Do NOT suggest breads (naan, roti) with rice dishes (biryani, pulao) — suggest a drink or dessert instead.

Stay strictly focused on food ordering for {name} only."""


def get_history(owner, customer_phone):
    key = (owner["id"], customer_phone)
    if key not in conversations:
        conversations[key] = {
            "messages":   [SystemMessage(content=build_system_prompt(owner))],
            "started_at": datetime.now().isoformat(),
        }
    return conversations[key]["messages"]


def clear_conversations():
    conversations.clear()


def chat(owner, customer_phone, incoming_msg):
    """Run one turn of the conversation. Returns the reply text (may be empty)."""
    history = get_history(owner, customer_phone)
    history.append(HumanMessage(content=incoming_msg))

    response = llm.invoke(history)
    reply    = response.content
    history.append(AIMessage(content=reply))

    # Menu image request
    if "[SEND_MENU]" in reply:
        if owner.get("menu_image_url"):
            whatsapp.send_image(owner, customer_phone, owner["menu_image_url"],
                                "Here's our menu. What would you like to order?")
            reply = reply.replace("[SEND_MENU]", "").strip()
        else:
            # No image configured — let the LLM list the menu in text instead
            history.append(HumanMessage(
                content="(No menu image is available. List the full menu in plain text.)"))
            reply = llm.invoke(history).content
            history.append(AIMessage(content=reply))

    reply = _extract_and_log_order(owner, reply, customer_phone)
    return reply


def format_bill(owner, order_data):
    lines = [
        "ORDER CONFIRMED",
        "=" * 28,
        f"Restaurant: {owner['restaurant_name']}",
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
        f"Thank you for ordering from {owner['restaurant_name']}!",
    ]
    return "\n".join(lines)


def notify_owner(owner, order_data):
    if not owner.get("admin_phone"):
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
    whatsapp.send_text(owner, owner["admin_phone"], "\n".join(lines))
    print(f"Owner notified at {owner['admin_phone']}")


def _extract_and_log_order(owner, reply_text, customer_phone):
    if "[ORDER_CONFIRMED]" not in reply_text:
        return reply_text
    try:
        parts       = reply_text.split("[ORDER_CONFIRMED]")
        clean_reply = parts[0].strip()
        json_part   = parts[1].strip().replace("```json", "").replace("```", "").strip()

        order_data = json.loads(json_part)
        order_data["phone"] = customer_phone

        save_order(
            owner_id = owner["id"],
            phone    = customer_phone,
            name     = order_data.get("name", "Guest"),
            address  = order_data.get("address", ""),
            total    = order_data.get("total", 0),
            items    = order_data.get("items", []),
        )
        print(f"ORDER SAVED TO DB: {order_data}")

        whatsapp.send_text(owner, customer_phone, format_bill(owner, order_data))

        try:
            notify_owner(owner, order_data)
        except Exception as e:
            print(f"Owner notify failed: {e}")

        return clean_reply

    except Exception as e:
        print(f"Order parse failed: {e}")
        return reply_text.split("[ORDER_CONFIRMED]")[0].strip()
