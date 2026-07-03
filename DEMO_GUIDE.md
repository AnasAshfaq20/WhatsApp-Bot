# LuxRide — WhatsApp Limo Booking Bot (Client Demo Guide)

Multi-tenant WhatsApp + voice booking platform for limousine / chauffeured car rental
businesses. Customers book over WhatsApp chat (text or voice notes) or by phone call;
bookings appear live on the dispatch dashboard.

## What changed from the restaurant version

| Restaurant | Limo platform |
|---|---|
| Menu items | Fleet (vehicles with capacity, hourly rate, min hours, airport flat rate) |
| Order (items + address) | Booking (vehicle, pickup date/time, pickup/drop-off, hours, passengers, occasion) |
| pending → preparing → delivered | pending → confirmed → en route → completed (+ cancelled) |
| Menu image | Fleet card image |
| `/orders` dashboard | `/bookings` dispatch dashboard with chauffeur assignment |
| `spicebot` package | `limobot` package |

## First deploy (IMPORTANT)

`init_db()` renames the old restaurant columns (`restaurant_name` → `business_name`, etc.)
on first start. **Deploy this code and restart the service in one step** — the old
restaurant code will stop working against the migrated database. For the cleanest demo,
point `DATABASE_URL` at a fresh database: the app auto-creates everything and seeds a
demo company (`luxride` / your `DASHBOARD_PASSWORD`) with a 9-vehicle fleet from
`fleet.json`.

If you keep the old database, log in to `/admin` and disable the old `spicegarden`
tenant so it doesn't clash with `luxride` on the same WhatsApp phone ID.

## Demo script (10 minutes)

1. **WhatsApp booking** — message the business number:
   - "Hi" → the bot greets as the LuxRide booking assistant
   - "I need a car for 6 people Saturday night" → it recommends the Escalade / Sprinter with rates
   - "Show me the fleet" → sends the fleet card image (upload one in `/admin` first)
   - Give pickup time, location, hours → it quotes a full price breakdown ($110 x 4 = $440)
   - It asks for your name, shows the complete summary, and only books after an explicit YES
   - Customer instantly receives a confirmation with a reference number (LX-0001)
2. **Voice notes** — send a WhatsApp voice message instead of typing; it's transcribed
   (Groq Whisper) and answered the same way.
3. **Dispatch dashboard** (`/bookings`) — the booking appears within 5 seconds:
   - Click **Confirm & Assign**, type a chauffeur name/phone → customer gets
     "booking confirmed, your chauffeur is Michael (+1555...)" on WhatsApp
   - **Chauffeur En Route** → customer notified to be ready
   - **Complete Trip** → thank-you message; **Cancel** at any stage notifies too
4. **Admin panel** (`/admin`) — the agency/SaaS angle:
   - Add a second limo company in 30 seconds (auto-generated login)
   - **Fleet** button → add/edit/hide vehicles and rates; the bot quotes new prices immediately in new chats
   - Per-company WhatsApp credentials, currency symbol, fleet image, voice number
5. **Phone bookings** — a Vapi (or similar) voice agent calls `POST /voice/booking`
   with the confirmed details; the booking lands on the same dashboard and the caller
   gets a WhatsApp confirmation.

## Pricing model the bot uses

- **Hourly charter**: `hourly_rate x hours`, enforcing each vehicle's `min_hours`
- **Flat-rate transfer** (airport etc.): the vehicle's `airport_rate`; vehicles with
  `airport_rate = 0` aren't offered for transfers
- The bot never invents vehicles or prices — it only quotes from the fleet table.

## Voice webhook payload (`POST /voice/booking`, header `X-Voice-Secret`)

```json
{
  "called_number": "+1 555 222 3333",
  "customer_phone": "15551234567",
  "name": "John Smith",
  "vehicle": "Cadillac Escalade",
  "booking_type": "transfer",
  "pickup_location": "JFK Terminal 4",
  "dropoff_location": "Hilton Midtown",
  "pickup_time": "Friday 6:00 PM",
  "hours": 0,
  "passengers": 3,
  "occasion": "airport transfer",
  "total": 150
}
```

Response includes `reference` (LX-####) and a `message` string the voice agent reads
back to the caller. The old `/voice/order` path still works as an alias.

## Tests

`uv run python test_e2e.py` — full end-to-end suite (auth, tenant isolation, real LLM
booking conversation, status flow + driver assignment, webhook, fleet CRUD, images,
voice webhook). It hits the configured database and Groq API, so run it against a
dev/demo `DATABASE_URL` only.
