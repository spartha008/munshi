"""Twilio WhatsApp channel (sandbox or a Twilio-provisioned sender). Used alongside Meta's Cloud API.

Env:
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN   from the Twilio console
  TWILIO_WHATSAPP_FROM                    e.g. whatsapp:+14155238886 (the sandbox number)
  TWILIO_SANDBOX_JOIN                     the sandbox's "join <word-word>" code, for the landing page link
  TWILIO_WEBHOOK_URL                      optional: the exact URL set in Twilio, used to check signatures
                                          when the server sits behind a tunnel or proxy

The sandbox is for testing: each phone must first send "join <code>" to the sandbox number,
and it has no interactive buttons, so button prompts are rendered as "Reply OK or Edit" lines.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

import httpx

from .channels import DeliveryError
from .whatsapp import WAMessage

TEXT_LIMIT = 1600
SANDBOX_NUMBER = "+14155238886"


def _cfg(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def configured() -> bool:
    return bool(_cfg("TWILIO_ACCOUNT_SID") and _cfg("TWILIO_AUTH_TOKEN") and _cfg("TWILIO_WHATSAPP_FROM"))


def is_sandbox() -> bool:
    return (_cfg("TWILIO_WHATSAPP_FROM") or "").replace("whatsapp:", "") == SANDBOX_NUMBER


def _auth() -> tuple[str, str]:
    return _cfg("TWILIO_ACCOUNT_SID") or "", _cfg("TWILIO_AUTH_TOKEN") or ""


# --------------------------------------------------------------------------- #
# Inbound
# --------------------------------------------------------------------------- #
def valid_signature(url: str, params: dict, header: str | None) -> bool:
    """X-Twilio-Signature = base64(HMAC-SHA1(auth token, url + sorted key+value pairs))."""
    token = _cfg("TWILIO_AUTH_TOKEN")
    if not token:
        return os.environ.get("MUNSHI_ALLOW_UNSIGNED") == "1"
    if not header:
        return False
    payload = url + "".join(k + str(params[k]) for k in sorted(params))
    digest = base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(digest, header)


def parse_form(form: dict) -> WAMessage | None:
    wa_id = (form.get("WaId") or re.sub(r"\D", "", form.get("From") or "")).strip()
    sid = form.get("MessageSid") or form.get("SmsMessageSid")
    if not wa_id or not sid:
        return None
    body = (form.get("Body") or "").strip()
    msg = WAMessage(wa_id=wa_id, message_id=sid, type="text", text=body, profile_name=form.get("ProfileName"),
                    channel="twilio")
    if int(form.get("NumMedia") or 0) > 0:
        ctype = (form.get("MediaContentType0") or "").lower()
        msg.media_id, msg.mime_type = form.get("MediaUrl0"), ctype
        if ctype.startswith("image/"):
            msg.type = "image"
        elif ctype.startswith("audio/"):
            msg.type = "audio"
        else:
            msg.type = "other"
    return msg


def download_media(url: str) -> tuple[bytes, str]:
    r = httpx.get(url, auth=_auth(), follow_redirects=True, timeout=60)
    r.raise_for_status()
    return r.content, (r.headers.get("content-type") or "application/octet-stream").split(";")[0]


# --------------------------------------------------------------------------- #
# Outbound
# --------------------------------------------------------------------------- #
def _post(to_wa_id: str, body: str) -> str:
    if not configured():
        raise DeliveryError("Twilio is not configured")
    sid, _ = _auth()
    try:
        r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", auth=_auth(),
                       data={"From": _cfg("TWILIO_WHATSAPP_FROM"), "To": f"whatsapp:+{to_wa_id.lstrip('+')}",
                             "Body": body}, timeout=30)
    except httpx.HTTPError as e:
        raise DeliveryError(f"network error: {e}") from e
    if r.status_code >= 400:
        try:
            err = r.json()
        except ValueError:
            err = {}
        raise DeliveryError(f"Twilio error {err.get('code')}: {err.get('message') or r.text[:200]}", err.get("code"))
    return r.json().get("sid", "")


def _chunks(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return [p[:limit] for p in parts]


def send_text(to: str, text: str) -> str:
    sid = ""
    for part in _chunks(text, TEXT_LIMIT):
        sid = _post(to, part)
    return sid


def buttons_as_text(buttons: list[tuple[str, str]]) -> str:
    """No interactive buttons on Twilio: tell the owner what to type instead."""
    ids = [b for b, _ in buttons]
    if ids and all(b.startswith("lang_") for b in ids):
        return "Reply 1 for English, 2 for ಕನ್ನಡ, 3 for हिंदी."
    words = {"ok": "OK", "edit": "Edit", "yes": "Yes", "no": "No"}
    return "Reply " + " or ".join(words.get(b, t) for b, t in buttons) + "."


def send_reply(to: str, reply) -> str:
    buttons = getattr(reply, "buttons", None)
    text = str(reply) + ("\n\n" + buttons_as_text(buttons) if buttons else "")
    return send_text(to, text)


def mark_read(message_id: str) -> None:
    return None  # Twilio marks sandbox messages read on its own


class Transport:
    """channels.Transport for messages Munshi starts (orders to distributors, notes to other shops)."""

    def send_order_to_distributor(self, to: str, shop_name: str, text: str, channel: str | None = None) -> str:
        if is_sandbox():
            # The sandbox only reaches phones that sent "join <code>"; a distributor won't have.
            # Twilio would accept the send and fail it later, so refuse now and let the owner forward it.
            raise DeliveryError("Twilio sandbox can only message phones that joined it", 63016)
        return send_text(to, text)

    def send_text(self, to: str, text: str, channel: str | None = None) -> str:
        return send_text(to, text)
