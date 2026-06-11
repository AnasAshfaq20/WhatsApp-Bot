"""Throwaway end-to-end test. WhatsApp sends are intercepted - nothing real goes out."""
from app import app
from spicebot.services import whatsapp, bot
from spicebot import db

# Intercept ALL outgoing WhatsApp calls
sent = []
whatsapp.send_text = lambda owner, to, body: sent.append(("text", owner["username"], to, body))
whatsapp.send_image = lambda owner, to, url, caption="": sent.append(("image", owner["username"], to, url))

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + str(detail) if detail and not cond else ""))


c = app.test_client()

# ==== 1. AUTH ====
r = c.post("/login", data={"username": "admin", "password": "WRONG"})
check("admin wrong password rejected", b"Invalid" in r.data)
r = c.post("/login", data={"username": "admin", "password": "spicegarden2024"})
check("admin login redirects to /admin", r.headers.get("Location") == "/admin")

# ==== 2. CREATE SECOND TENANT ====
r = c.post("/admin/owners", json={
    "username": "karachigrill", "owner_name": "Bilal", "restaurant_name": "Karachi Grill",
    "hours": "12 PM - 12 AM", "location": "Clifton, Karachi", "delivery_info": "Rs 100 delivery fee",
    "whatsapp_phone_id": "5550001111", "whatsapp_token": "FAKE_TOKEN", "admin_phone": "923001112222",
})
res = r.json
kg_id = res["owner_id"]
kg_pw = res["credentials"]["password"]
check("create 2nd owner", res["success"])

r = c.post("/admin/owners", json={"username": "", "owner_name": "", "restaurant_name": ""})
check("empty owner form rejected", not r.json["success"])

# ==== 3. NEW OWNER: EMPTY MENU PROMPT BUILDS ====
kg = db.get_owner_by_id(kg_id)
prompt = bot.build_system_prompt(kg)
check("prompt builds for owner with empty menu", "Karachi Grill" in prompt and "Clifton" in prompt)
check("prompt has correct delivery info", "Rs 100 delivery fee" in prompt)
check("no Spice Garden leakage in 2nd owner prompt", "Spice Garden" not in prompt)

# ==== 4. REAL LLM CONVERSATION (spicegarden, sends intercepted) ====
sg = db.get_owner_by_id(1)
reply1 = bot.chat(sg, "92399TEST", "hi")
check("LLM greets", isinstance(reply1, str) and len(reply1) > 0, reply1)

reply2 = bot.chat(sg, "92399TEST", "yes show me the menu")
img_sends = [s for s in sent if s[0] == "image"]
check("menu request triggers image send", len(img_sends) == 1, sent)
check("[SEND_MENU] tag stripped from reply", "[SEND_MENU]" not in (reply2 or ""))

reply3 = bot.chat(sg, "92399TEST", "i want 2 chicken biryani and 1 pepsi")
check("order repeated with prices", "Rs" in reply3, reply3)

reply4 = bot.chat(sg, "92399TEST", "yes confirm my order")
check("asks for name+address at confirm",
      "name" in reply4.lower() and "address" in reply4.lower(), reply4)

before = len(db.get_orders_for_owner(1))
reply5 = bot.chat(sg, "92399TEST", "My name is Test Bilal, address House 9 DHA Phase 5 Lahore")
orders = db.get_orders_for_owner(1)
check("order saved to DB", len(orders) == before + 1, f"{before} -> {len(orders)}")
check("[ORDER_CONFIRMED] tag not leaked", "[ORDER_CONFIRMED]" not in (reply5 or ""))

new_order = orders[-1]
check("order has correct owner scoping", new_order["owner_id"] == 1)
check("order name captured", "Bilal" in new_order["name"], new_order["name"])
check("order items parsed", len(new_order["items"]) >= 2, new_order["items"])

bills = [s for s in sent if s[0] == "text" and "ORDER CONFIRMED" in s[3]]
check("bill sent to customer", any(s[2] == "92399TEST" for s in bills))
admin_alerts = [s for s in sent if s[0] == "text" and "NEW ORDER RECEIVED" in s[3]]
check("owner admin alerted", any(s[2] == sg["admin_phone"] for s in admin_alerts))

# ==== 5. TENANT ISOLATION ====
check("conversations keyed per owner",
      (1, "92399TEST") in bot.conversations and (kg_id, "92399TEST") not in bot.conversations)
check("2nd owner has zero orders", len(db.get_orders_for_owner(kg_id)) == 0)

o = db.get_owner_by_phone_id("5550001111")
check("webhook lookup finds 2nd owner", o is not None and o["username"] == "karachigrill")

# ==== 6. OWNER DASHBOARD SCOPING ====
c2 = app.test_client()
c2.post("/login", data={"username": "karachigrill", "password": kg_pw})
data = c2.get("/orders/data").json
check("owner sees ONLY own (zero) orders", data["orders"] == [])

r = c2.post("/orders/update", json={"id": new_order["id"], "status": "delivered"})
check("owner blocked from updating other owner's order", not r.json["success"], r.json)
fresh = [x for x in db.get_orders_for_owner(1) if x["id"] == new_order["id"]][0]
check("order status untouched", fresh["status"] == "pending", fresh["status"])

# ==== 7. STATUS UPDATE + CUSTOMER NOTIFY (admin scope) ====
sent.clear()
r = c.post("/orders/update", json={"id": new_order["id"], "status": "preparing"})
check("admin updates any order", r.json["success"])
check("customer notified of preparing", any("being prepared" in s[3] for s in sent), sent)

# ==== 8. DISABLED OWNER ====
c.put(f"/admin/owners/{kg_id}", json={"active": False})
c3 = app.test_client()
r = c3.post("/login", data={"username": "karachigrill", "password": kg_pw})
check("disabled owner cannot log in", b"Invalid" in r.data)
check("disabled owner webhook dropped", db.get_owner_by_phone_id("5550001111") is None)

# ==== 9. FULL WEBHOOK PATH (2nd owner re-enabled) ====
c.put(f"/admin/owners/{kg_id}", json={"active": True})
sent.clear()
payload = {"entry": [{"changes": [{"value": {
    "metadata": {"phone_number_id": "5550001111"},
    "messages": [{"from": "92388WEBHOOK", "type": "text", "id": "wbtest1", "text": {"body": "hello"}}]
}}]}]}
r = c.post("/webhook", json=payload)
check("webhook 200", r.status_code == 200)
check("webhook replied via 2nd owner",
      any(s[1] == "karachigrill" and s[2] == "92388WEBHOOK" for s in sent), sent)

sent.clear()
c.post("/webhook", json=payload)
check("duplicate message id ignored", len(sent) == 0, sent)

payload2 = {"entry": [{"changes": [{"value": {
    "metadata": {"phone_number_id": "5550001111"},
    "messages": [{"from": "92388WEBHOOK", "type": "sticker", "id": "wbtest2"}]
}}]}]}
sent.clear()
c.post("/webhook", json=payload2)
check("unsupported type gets polite reply", any("text and voice" in s[3] for s in sent))

# ==== 10. CLEANUP TEST DATA ====
c.delete(f"/admin/owners/{kg_id}")
check("2nd owner deleted", db.get_owner_by_id(kg_id) is None)
conn = db.get_db()
cur = conn.cursor()
cur.execute("DELETE FROM orders WHERE phone = '92399TEST'")
conn.commit()
conn.close()
check("test order cleaned up", all(o["phone"] != "92399TEST" for o in db.get_orders_for_owner(1)))

print()
print(f"RESULT: {len(passed)} passed, {len(failed)} failed")
if failed:
    print("FAILED:", failed)
