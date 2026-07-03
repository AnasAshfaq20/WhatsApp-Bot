"""Throwaway end-to-end test (FastAPI). WhatsApp sends are intercepted - nothing real goes out.

NOTE: importing app runs init_db() against DATABASE_URL, which applies the
restaurant -> limo schema migrations. Only run this against a dev/demo database.
"""
import io
import json

from fastapi.testclient import TestClient

from app import app
from limobot.services import whatsapp, bot
from limobot import db, config

# Intercept ALL outgoing WhatsApp calls
sent = []
whatsapp.send_text = lambda owner, to, body: sent.append(("text", owner["username"], to, body))
whatsapp.send_image = lambda owner, to, url, caption="": sent.append(("image", owner["username"], to, url))

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + str(detail) if detail and not cond else ""))


# follow_redirects=False so we can inspect the 303 Location like the Flask test did
c = TestClient(app, follow_redirects=False)

# ==== 1. AUTH ====
r = c.post("/login", data={"username": "admin", "password": "WRONG"})
check("admin wrong password rejected", b"Invalid" in r.content)
r = c.post("/login", data={"username": "admin", "password": config.ADMIN_PASSWORD})
check("admin login redirects to /admin", r.headers.get("location") == "/admin")

# ==== 2. CREATE SECOND TENANT ====
r = c.post("/admin/owners", json={
    "username": "elitelimos", "owner_name": "James", "business_name": "Elite Limousines",
    "hours": "24/7", "location": "Midtown garage", "service_area": "City + all airports",
    "currency": "$", "whatsapp_phone_id": "5550001111", "whatsapp_token": "FAKE_TOKEN",
    "admin_phone": "15551112222",
})
res = r.json()
el_id = res["owner_id"]
el_pw = res["credentials"]["password"]
check("create 2nd owner", res["success"])

r = c.post("/admin/owners", json={"username": "", "owner_name": "", "business_name": ""})
check("empty owner form rejected", not r.json()["success"])

# ==== 3. FLEET MANAGEMENT ====
r = c.post(f"/admin/owners/{el_id}/vehicles", json={
    "category": "Luxury SUVs", "name": "Lincoln Navigator", "capacity": 6,
    "hourly_rate": 105, "min_hours": 2, "airport_rate": 145,
    "description": "Test SUV"})
check("vehicle added", r.json()["success"], r.json())
vehicles = c.get(f"/admin/owners/{el_id}/vehicles").json()["vehicles"]
check("vehicle listed", len(vehicles) == 1 and vehicles[0]["name"] == "Lincoln Navigator")
veh_id = vehicles[0]["id"]
r = c.put(f"/admin/owners/{el_id}/vehicles/{veh_id}", json={"hourly_rate": 115})
check("vehicle rate updated", r.json()["success"])
vehicles = c.get(f"/admin/owners/{el_id}/vehicles").json()["vehicles"]
check("vehicle update persisted", vehicles[0]["hourly_rate"] == 115)

# ==== 4. NEW OWNER PROMPT BUILDS ====
el = db.get_owner_by_id(el_id)
prompt = bot.build_system_prompt(el)
check("prompt builds for 2nd owner", "Elite Limousines" in prompt and "Midtown" in prompt)
check("prompt includes fleet vehicle", "Lincoln Navigator" in prompt)
check("no LuxRide leakage in 2nd owner prompt", "LuxRide" not in prompt)

# ==== 5. REAL LLM CONVERSATION (luxride, sends intercepted) ====
lux = db.get_owner_by_username("luxride")
lux_id = lux["id"]
reply1 = bot.chat(lux, "92399TEST", "hi")
check("LLM greets", isinstance(reply1, str) and len(reply1) > 0, reply1)

reply2 = bot.chat(lux, "92399TEST", "yes show me the fleet")
check("[SEND_FLEET] tag stripped from reply", "[SEND_FLEET]" not in (reply2 or ""))

reply3 = bot.chat(lux, "92399TEST",
                  "I need an SUV for 5 people this Saturday 7pm, pickup at Grand Hotel, "
                  "4 hours for a night out")
check("quote includes a price", "$" in reply3, reply3)

before = len(db.get_bookings_for_owner(lux_id))
bot.chat(lux, "92399TEST", "My name is Test Bilal, let's go with the Escalade")
# explicit confirmation now required
reply5 = bot.chat(lux, "92399TEST", "yes please confirm the booking")
bookings = db.get_bookings_for_owner(lux_id)
check("booking saved to DB after explicit yes", len(bookings) == before + 1, f"{before} -> {len(bookings)}")
check("[BOOKING_CONFIRMED] tag not leaked", "[BOOKING_CONFIRMED]" not in (reply5 or ""))

new_booking = bookings[-1]
check("booking has correct owner scoping", new_booking["owner_id"] == lux_id)
check("booking name captured", "Bilal" in new_booking["name"], new_booking["name"])
check("booking vehicle captured", new_booking["vehicle"], new_booking)
check("booking pickup captured", "Grand Hotel" in new_booking["pickup_location"], new_booking)
check("booking has reference", new_booking["ref"].startswith("LX-"), new_booking["ref"])
check("timestamp stored in UTC", "+00:00" in new_booking["timestamp"], new_booking["timestamp"])

confs = [s for s in sent if s[0] == "text" and "BOOKING CONFIRMED" in s[3]]
check("confirmation sent to customer", any(s[2] == "92399TEST" for s in confs))
admin_alerts = [s for s in sent if s[0] == "text" and "NEW BOOKING RECEIVED" in s[3]]
check("owner admin alerted", any(s[2] == lux["admin_phone"] for s in admin_alerts))

# ==== 6. TENANT ISOLATION ====
check("conversations keyed per owner",
      (lux_id, "92399TEST") in bot.conversations and (el_id, "92399TEST") not in bot.conversations)
check("2nd owner has zero bookings", len(db.get_bookings_for_owner(el_id)) == 0)

o = db.get_owner_by_phone_id("5550001111")
check("webhook lookup finds 2nd owner", o is not None and o["username"] == "elitelimos")

# ==== 7. OWNER DASHBOARD SCOPING ====
c2 = TestClient(app, follow_redirects=False)
c2.post("/login", data={"username": "elitelimos", "password": el_pw})
data = c2.get("/bookings/data").json()
check("owner sees ONLY own (zero) bookings", data["bookings"] == [])

r = c2.post("/bookings/update", json={"id": new_booking["id"], "status": "completed"})
check("owner blocked from updating other owner's booking", not r.json()["success"], r.json())
fresh = [x for x in db.get_bookings_for_owner(lux_id) if x["id"] == new_booking["id"]][0]
check("booking status untouched", fresh["status"] == "pending", fresh["status"])

# ==== 8. STATUS FLOW + DRIVER ASSIGN + CUSTOMER NOTIFY (admin scope) ====
sent.clear()
r = c.post("/bookings/update", json={"id": new_booking["id"], "status": "confirmed",
                                     "driver_name": "Michael", "driver_phone": "15550009999"})
check("admin confirms any booking", r.json()["success"])
check("customer notified of confirmation with driver",
      any("CONFIRMED" in s[3] and "Michael" in s[3] for s in sent), sent)
sent.clear()
r = c.post("/bookings/update", json={"id": new_booking["id"], "status": "en_route"})
check("en_route notify", any("on the way" in s[3] for s in sent), sent)
r = c.post("/bookings/update", json={"id": new_booking["id"], "status": "not_a_status"})
check("invalid status rejected", not r.json()["success"])

# ==== 9. DISABLED OWNER ====
c.put(f"/admin/owners/{el_id}", json={"active": False})
c3 = TestClient(app, follow_redirects=False)
r = c3.post("/login", data={"username": "elitelimos", "password": el_pw})
check("disabled owner cannot log in", b"Invalid" in r.content)
check("disabled owner webhook dropped", db.get_owner_by_phone_id("5550001111") is None)

# ==== 10. FULL WEBHOOK PATH (2nd owner re-enabled) ====
c.put(f"/admin/owners/{el_id}", json={"active": True})
sent.clear()
payload = {"entry": [{"changes": [{"value": {
    "metadata": {"phone_number_id": "5550001111"},
    "messages": [{"from": "92388WEBHOOK", "type": "text", "id": "wbtest1", "text": {"body": "hello"}}]
}}]}]}
r = c.post("/webhook", json=payload)
check("webhook 200", r.status_code == 200)
check("webhook replied via 2nd owner",
      any(s[1] == "elitelimos" and s[2] == "92388WEBHOOK" for s in sent), sent)

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

# ==== 11. WEBHOOK VERIFY (GET) ====
r = c.get("/webhook", params={"hub.mode": "subscribe",
                              "hub.verify_token": config.VERIFY_TOKEN,
                              "hub.challenge": "12345"})
check("webhook GET verify echoes challenge", r.text == "12345", r.text)

# ==== 12. FLEET IMAGE UPLOAD + PUBLIC SERVE ====
png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
                    "0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082")
r = c.post(f"/admin/owners/{el_id}/fleet-image",
           files={"image": ("fleet.png", io.BytesIO(png), "image/png")})
check("fleet image upload", r.json()["success"], r.json())
r = TestClient(app).get(f"/fleet-image/{el_id}")
check("fleet image served publicly", r.status_code == 200 and r.content == png)
r = c.post(f"/admin/owners/{el_id}/fleet-image",
           files={"image": ("evil.exe", io.BytesIO(b"x"), "application/octet-stream")})
check("bad image type rejected", not r.json()["success"])

# ==== 13. VOICE BOOKING ====
db.update_owner(el_id, {"voice_phone": "+1 555 222 3333"})
H = {"X-Voice-Secret": config.VOICE_WEBHOOK_SECRET}
r = c.post("/voice/booking", json={"called_number": "15552223333"})  # no secret
check("voice booking without secret -> 401", r.status_code == 401)
vbefore = len(db.get_bookings_for_owner(el_id))
r = c.post("/voice/booking", headers=H, json={
    "called_number": "+1 555 222 3333", "customer_phone": "92366VOICE", "name": "Voice Caller",
    "vehicle": "Lincoln Navigator", "booking_type": "transfer",
    "pickup_location": "JFK Terminal 4", "dropoff_location": "Hilton Midtown",
    "pickup_time": "Friday 6:00 PM", "passengers": 3, "total": 145})
check("voice booking placed", r.json().get("success") is True, r.json())
check("voice booking has reference", r.json().get("reference", "").startswith("LX-"), r.json())
check("voice booking saved", len(db.get_bookings_for_owner(el_id)) == vbefore + 1)
r = c.post("/voice/booking", headers=H, json={"called_number": "15552223333",
                                              "vehicle": "Lincoln Navigator"})
check("voice booking missing fields -> 400", r.status_code == 400)
r = c.post("/voice/booking", headers=H, json={
    "called_number": "9999999999", "vehicle": "x",
    "pickup_location": "y", "pickup_time": "z"})
check("voice unknown number -> 404", r.status_code == 404)

# ==== 14. CLEANUP TEST DATA ====
c.delete(f"/admin/owners/{el_id}")
check("2nd owner deleted", db.get_owner_by_id(el_id) is None)
conn = db.get_db()
cur = conn.cursor()
cur.execute("DELETE FROM bookings WHERE phone IN ('92399TEST','92366VOICE')")
conn.commit()
conn.close()
check("test booking cleaned up",
      all(b["phone"] != "92399TEST" for b in db.get_bookings_for_owner(lux_id)))

print()
print(f"RESULT: {len(passed)} passed, {len(failed)} failed")
if failed:
    print("FAILED:", failed)
