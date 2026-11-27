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

1. On developers.facebook.com, create an app and add the **WhatsApp** product. Meta gives you a test
   business number.
2. **WhatsApp > API Setup**: copy the **Phone number ID** (`WHATSAPP_PHONE_NUMBER_ID`).
3. **Test number limits:** Meta's test number only delivers to recipient numbers you add on the API Setup
   page (a handful). Add your team's and the judges' phones there. For a number anyone can message, add your
   own business phone number on the same page and complete the display-name review.
4. **Permanent token:** in Meta Business settings, create a **System user**, give it the app and the
   WhatsApp account, and generate a token with `whatsapp_business_messaging` and
   `whatsapp_business_management`. That's `WHATSAPP_TOKEN`. (The 24-hour token on the API Setup page works
   for a quick test but expires.)
5. **App settings > Basic**: copy the **App secret** (`WHATSAPP_APP_SECRET`). Munshi rejects any webhook
   call that isn't signed with it.
6. `WHATSAPP_DISPLAY_NUMBER` is the number as you want it shown on the landing page and QR code,
   e.g. `+91 98xxx xxxxx`.

## 3. Deploy on Render

1. Push this repository to GitHub.
2. In Render: **New > Blueprint**, pick the repo. Render reads `render.yaml`: one Docker web service in
   Singapore with a 1 GB disk at `/var/data` (plan `0.5c-512mb`; persistent disks need a paid instance).
3. Fill in the values it asks for, copying each from your local `.env` (including
   `WHATSAPP_VERIFY_TOKEN`, so the webhook keeps working). `ADMIN_PASSWORD` is generated for you; read
   it from the service's **Environment** tab afterwards.
4. Wait for the deploy, then open `https://<your-service>.onrender.com/healthz`. It should return
   `{"ok": true, "whatsapp": true, "claude": true, "voice": true}`.

The service must run as a single instance with one worker (the Dockerfile does this): the message worker
and scheduler run in-process against one SQLite file. Render snapshots the disk daily.

## 4. Point WhatsApp at Munshi

In the Meta app: **WhatsApp > Configuration > Webhook**

- Callback URL: `https://<your-service>.onrender.com/webhook`
- Verify token: the `WHATSAPP_VERIFY_TOKEN` value from Render
- Click **Verify and save**, then subscribe to the **messages** webhook field.

Send "Hi" to the number from a phone. Munshi asks for a language and five short setup questions, then
it's ready for bill photos.

## 4b. Optional: Twilio as a second WhatsApp channel

Meta sometimes locks a brand-new business account for review (delivery error 131031) for a day or
two. Twilio's WhatsApp sandbox works within minutes and can run alongside Meta; Munshi replies to
each owner on the channel they wrote in on.

1. Create a free account at twilio.com. On the Console home, copy **Account SID** and **Auth Token**
   into `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN`.
2. **Messaging → Try it out → Send a WhatsApp message**: note the sandbox number (`+1 415 523 8886`)
   and its join code ("join <word-word>"). Set `TWILIO_WHATSAPP_FROM=whatsapp:+14155238886` and
   `TWILIO_SANDBOX_JOIN=<word-word>`.
3. **Sandbox settings**: "When a message comes in" = `https://<your host>/twilio`, method POST. Save.
4. Each tester sends `join <word-word>` to the sandbox number once; the landing page link pre-fills
   it when `MUNSHI_PUBLIC_CHANNEL=twilio`.

Sandbox limits: no reply buttons (Munshi writes "Reply OK or Edit" instead), only joined phones can be
messaged (so distributor orders are handed to the owner to forward), and the join expires after 72 hours
of silence. A Twilio-provisioned production sender removes all three.

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
- Back up `/var/data/munshi.db` with `sqlite3 /var/data/munshi.db ".backup /var/data/backup.db"` from the
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
