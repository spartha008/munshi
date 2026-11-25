"""Send a signed, Meta-shaped webhook to a running Munshi, to try it without a phone.

  python scripts/send_test_message.py "Hi"
  python scripts/send_test_message.py "Maggi khatam, Parle-G do bache" --from 919000000001
  python scripts/send_test_message.py --button ok

Uses WHATSAPP_APP_SECRET from the environment to sign. Replies go out through the WhatsApp API
(so they only arrive if WHATSAPP_TOKEN is set); read them in the console at /admin.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
import uuid

import httpx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", default="Hi")
    ap.add_argument("--from", dest="wa_id", default="919000000001", help="sender's WhatsApp number, digits only")
    ap.add_argument("--name", default="Test Owner")
    ap.add_argument("--button", help="send a reply-button tap with this id instead of text (ok, edit, yes, no)")
    ap.add_argument("--url", default="http://localhost:8000/webhook")
    a = ap.parse_args()

    msg = {"from": a.wa_id, "id": f"wamid.test.{uuid.uuid4().hex}", "timestamp": str(int(time.time()))}
    if a.button:
        msg.update(type="interactive", interactive={"type": "button_reply",
                                                    "button_reply": {"id": a.button, "title": a.button}})
    else:
        msg.update(type="text", text={"body": a.text})
    body = json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "0", "changes": [{
        "field": "messages", "value": {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "0"},
                                       "contacts": [{"wa_id": a.wa_id, "profile": {"name": a.name}}],
                                       "messages": [msg]}}]}]}).encode()
    secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    r = httpx.post(a.url, content=body, headers=headers, timeout=30)
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
