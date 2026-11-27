# Deploying Munshi

Munshi is one Docker service: a FastAPI app that receives WhatsApp messages on a webhook, runs the agent,
and keeps each shop's ledger in SQLite on a persistent disk. It needs four accounts:

| Service | Used for | Env vars |
|---|---|---|
| Anthropic (Claude API) | reading bill photos, matching item names, Kannada/Hindi replies | `ANTHROPIC_API_KEY` |
| Meta WhatsApp Cloud API | the WhatsApp number owners talk to | `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_DISPLAY_NUMBER` |
| Sarvam AI | transcribing voice notes (Kannada, Hindi, code-mixed) | `SARVAM_API_KEY` |
| Render | hosting, HTTPS, persistent disk | set in the Render dashboard |

Plus `ADMIN_PASSWORD` for the operator console at `/admin` (user `admin`).

---

## 1. Get the API keys

**Claude.** In the Anthropic Console, create an API key and add prepaid credit. Munshi uses two models:
`claude-opus-5-5` to read bill photos (`MUNSHI_MODEL`) and `claude-haiku-5-5` for name matching and
Kannada/Hindi replies (`MUNSHI_MODEL_TEXT`). At the README's volumes that is roughly $2.50 per shop per
month; $5 of credit covers a week of testing.

**Sarvam.** In the Sarvam dashboard, create an API subscription key. Munshi calls `POST /speech-to-text`
with model `saaras:v4` in `translit` mode, so transcripts come back in Latin script.

## 2. Set up the WhatsApp number (Meta)

Use a Facebook account with some history. A brand-new business portfolio can be restricted on creation
("Account restricted — verify your business"; every send then fails with error 131031), and the only way
out of that is Meta Business Verification with company documents. An established account usually gets a
working test number straight away.

1. On developers.facebook.com → **My Apps → Create app**, pick the WhatsApp use case (or Other →
   Business), and let it create a **new** business portfolio. In business.facebook.com → Settings →
   **Business info**, fill Legal name, Country and Website right away; Meta's checks don't run on an
   incomplete profile.
2. In the app, **Step 1. Try it out** shows the test number, its **Phone number ID**
   (`WHATSAPP_PHONE_NUMBER_ID`) and the **WhatsApp Business Account ID**. The test number is
   `WHATSAPP_DISPLAY_NUMBER` until you register a real one.
3. **Recipients:** the test number only delivers to phones added under **To → Manage phone number list**
   (OTP each; up to 5). Add the team's and the judges' phones, and the distributor's if orders should reach
   them directly. For a number anyone can message, add your own business number under Step 2 and
   complete the display-name review.
4. **Token:** the token on the Step 1 page lasts 24 hours and dies when that browser session logs out. For
   anything beyond a quick test, create a **System user** (Business settings → Users → System users), give
   it the app and the WhatsApp account, and generate a token with `whatsapp_business_messaging`,
   `whatsapp_business_management` and `business_management`, expiry Never. That's `WHATSAPP_TOKEN`.
5. **App settings > Basic**: copy the **App secret** (`WHATSAPP_APP_SECRET`). Munshi rejects any webhook
   call that isn't signed with it.
6. **Subscribe the app to the account.** The new developer flow does not always do this, and without it
   Meta never forwards messages even though the webhook verifies. Check with
   `GET /{WABA_ID}/subscribed_apps`; if the app isn't listed, run
   `POST https://graph.facebook.com/v25.0/{WABA_ID}/subscribed_apps` with the token as a Bearer header.
7. Pay-as-you-go: Meta's health check (`GET /{WABA_ID}?fields=health_status`) flags a missing payment
   method; replies to owners who message first are free, but adding a small prepaid amount under
   Billing & payments clears the flag.

## 3. Deploy on Render

1. Push this repository to GitHub.
2. In Render: **New > Blueprint**, pick the repo. Render reads `render.yaml`: one Docker web service in
   Singapore with a 1 GB disk at `/var/data` (plan `0.5c-512mb`; persistent disks need a paid instance).
3. Fill in the values it asks for, copying each from your local `.env` (including
   `WHATSAPP_VERIFY_TOKEN`, so the webhook keeps working). `ADMIN_PASSWORD` is generated for you; read
   it from the service's **Environment** tab afterwards.
4. Wait for the deploy, then open `https://<your-service>.onrender.com/healthz`. It should return
   `{"ok": true, "whatsapp": true, "twilio": false, "claude": true, "voice": true}`.

The service must run as a single instance with one worker (the Dockerfile does this): the message worker
and scheduler run in-process against one SQLite file. Render snapshots the disk daily.

## 4. Point WhatsApp at Munshi

In the Meta app: **Step 2. Production setup → Configure Webhooks** (or the app's **Webhooks** page with
"WhatsApp Business Account" selected):

- Callback URL: `https://<your-service>.onrender.com/webhook`
- Verify token: your `WHATSAPP_VERIFY_TOKEN` value
- Click **Verify and save**, then subscribe to the **messages** field.

The same can be done without the UI, with the app token `{APP_ID}|{APP_SECRET}`:
`POST https://graph.facebook.com/v25.0/{APP_ID}/subscriptions` with
`object=whatsapp_business_account`, `callback_url`, `verify_token`, `fields=messages`.

Send "Hi" to the number from a registered phone. Munshi asks for a language and five short setup
questions, then it's ready for bill photos. If a reply doesn't arrive, the server log and the console's
trace show Meta's delivery report (`whatsapp_delivery_failed`) with the error code.

## 4b. Optional: Twilio as a second WhatsApp channel

Munshi can also run on a Twilio WhatsApp sender, alongside Meta; each owner is answered on the channel
they wrote in on. Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_WHATSAPP_FROM`
(`whatsapp:+<number>`), and point the sender's inbound webhook at `https://<your host>/twilio` (POST).
Twilio signs each call; Munshi checks the signature against the public URL (set `TWILIO_WEBHOOK_URL` if
the service sits behind a proxy that rewrites it).

Two cautions. Twilio's old free **WhatsApp Sandbox is not available to accounts created in its new
Console** (the trial "Try out WhatsApp" can only send Twilio's own templates, so it cannot carry Munshi's
replies); the adapter still supports the sandbox for older accounts that have it. And a Twilio-provisioned
sender needs the same Meta business verification as the Cloud API, so Twilio is not a way around that.

## 5. Optional: message templates

WhatsApp lets a business send free-form messages only within 24 hours of the customer's last message.
Munshi works within that:

- **Orders to distributors.** If the distributor hasn't messaged the number in 24 hours, the owner gets a
  ready-to-forward copy of the order instead. To send directly anyway, create an approved Utility template
  with two body variables (shop name, order text) and set `WHATSAPP_ORDER_TEMPLATE` to its name.
- **Salesman-morning orders and reminders.** If the owner has been quiet for 24 hours, the order is still
  drafted and waits for them. To ping them, create a Utility template with one variable (shop name), e.g.
  "Namaskara {{1}}, the order for today's salesman is ready. Reply *order* to see it.", and set
  `WHATSAPP_NUDGE_TEMPLATE`.

Set `WHATSAPP_TEMPLATE_LANG` if the templates aren't in `en`.

## 6. Operate

- `/admin` shows every shop: the conversation, each tool call the agent made, the ledger, orders, expiry
  risk and the shop's learned vocabulary. Owners never see it.
- Logs are in the Render dashboard. A failed send or a declined Claude call is logged with the reason.
- Back up `/var/data/munshi.db` from the Render shell (the image has no `sqlite3` binary, so use Python):
  `python -c "import sqlite3; sqlite3.connect('/var/data/munshi.db').backup(sqlite3.connect('/var/data/backup.db'))"`, run from the
  Render shell.

## Local development

```
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt     # Windows (bin/ on macOS/Linux)
.venv/Scripts/python -m pytest
cp .env.example .env    # fill in keys
.venv/Scripts/python -m uvicorn src.server:app --port 8000 --env-file .env
```

To talk to a local server without Meta, sign and post a fake message:

```
python scripts/send_test_message.py "Hi" --from 919000000001
```

To receive real WhatsApp messages locally, expose port 8000 with a tunnel (for example `cloudflared
tunnel --url http://localhost:8000`) and use the tunnel URL as the webhook callback.
