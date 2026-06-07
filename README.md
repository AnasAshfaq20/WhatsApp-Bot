# WhatsApp Restaurant Bot — Demo

A working WhatsApp ordering bot in ~200 lines of Python. Uses Twilio Sandbox (no business verification needed) and Google Gemini for natural language understanding.

## What it does

- Greets customers
- Shows menu, answers questions ("do you have anything vegetarian?")
- Takes orders in natural language ("2 chicken biryanis and a Pepsi")
- Confirms order with itemized total
- Asks for delivery address
- Logs the final order to `orders.json`

## Setup (15 minutes)

### 1. Get a Gemini API key (free)

Go to https://aistudio.google.com/app/apikey → Create API Key → copy it.

### 2. Set up Twilio WhatsApp Sandbox (free)

1. Sign up at https://twilio.com (no credit card needed for sandbox)
2. Go to **Messaging → Try it out → Send a WhatsApp message**
3. You'll see a Twilio number and a join code like `join orange-tiger`
4. From your phone's WhatsApp, send `join orange-tiger` to that Twilio number
5. You're now connected to the sandbox

### 3. Install and run with uv

```bash
cd whatsapp-restaurant-bot

# uv reads pyproject.toml, creates .venv, installs deps — all in one step
uv sync

cp .env.example .env
# open .env and paste your GEMINI_API_KEY

# Run the app (uv run uses the project's venv automatically)
uv run python app.py
```

The server is now running at `http://localhost:5000`.

> If you'd rather build the project from scratch instead of using the included pyproject.toml:
> ```bash
> uv init
> uv add flask twilio google-generativeai python-dotenv
> uv run python app.py
> ```

### 4. Expose your local server with ngrok

Twilio needs a public URL to send webhooks to. Ngrok gives you one in seconds.

```bash
# install ngrok from https://ngrok.com/download
ngrok http 5000
```

You'll get a URL like `https://abc123.ngrok-free.app`. Copy it.

### 5. Connect Twilio to your bot

1. In the Twilio console: **Messaging → Try it out → WhatsApp Sandbox Settings**
2. Set **"When a message comes in"** to: `https://abc123.ngrok-free.app/whatsapp`
3. Method: `POST`
4. Save

### 6. Test it

Send any message from your WhatsApp to the Twilio sandbox number. You should get a response from your bot.

## Demo script

A flow that always lands well:

1. **You:** "hi"
   → Bot greets, lists categories
2. **You:** "show me the biryanis"
   → Bot lists biryani options with prices
3. **You:** "I'll take 2 chicken biryanis, 1 chicken karahi half, and 4 garlic naans"
   → Bot repeats order with line items + total
4. **You:** "yes confirm"
   → Bot asks for delivery address
5. **You:** "House 12, Block B, DHA Phase 5"
   → Bot confirms order, gives ETA
6. Open `http://localhost:5000/orders` on your laptop → show the live-logged order

That last step — flipping to the orders endpoint to show the data persisted — is the moment that sells the demo.

## Useful endpoints during the demo

- `GET /orders` — view all orders received
- `GET /reset` — clear conversation history (use this between demo runs so each one starts fresh)
- `GET /` — health check

## Customizing for the actual restaurant

Edit `menu.json`. That's it. Restaurant name, hours, location, all menu items and prices — all driven from that one file.

## Going to production later

When the demo lands and you need to ship for real:

1. **Switch Twilio Sandbox → Meta WhatsApp Cloud API** with the restaurant's verified business number
2. **Move conversations from in-memory dict → Firestore or Redis** (so restarts don't kill active orders)
3. **Replace `orders.json` with Firestore** and add a simple kitchen dashboard (Streamlit works fast)
4. **Add payment** — JazzCash/Easypaisa/Stripe link sent in the chat
5. **Deploy** — Cloud Run is the cleanest fit since you're already on GCP

## Files

- `app.py` — the whole bot
- `menu.json` — restaurant data
- `orders.json` — auto-created when first order is placed
- `.env` — secrets (copy from `.env.example`)
- `pyproject.toml` — uv project config