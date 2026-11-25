"""WhatsApp Cloud API (Meta): webhook verification, signature check, parsing, media, sending.

Env:
  WHATSAPP_TOKEN            permanent access token (System User token)
  WHATSAPP_PHONE_NUMBER_ID  the sending number's ID from the WhatsApp > API Setup page
  WHATSAPP_APP_SECRET       App secret, used to verify X-Hub-Signature-256
  WHATSAPP_VERIFY_TOKEN     any string; the same value goes in the Meta webhook settings
  WHATSAPP_API_VERSION      Graph API version (default v25.0)
  WHATSAPP_ORDER_TEMPLATE   optional approved template for orders to distributors outside the 24-hour window
  WHATSAPP_NUDGE_TEMPLATE   optional approved template to reopen a chat with an owner (scheduled messages)
  WHATSAPP_TEMPLATE_LANG    template language code (default en)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass

import httpx

from .channels import OUTSIDE_WINDOW_ERRORS, DeliveryError

log = logging.getLogger("munshi.whatsapp")

TEXT_LIMIT = 4096
BUTTON_BODY_LIMIT = 1024


def _cfg(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def configured() -> bool:
    return bool(_cfg("WHATSAPP_TOKEN") and _cfg("WHATSAPP_PHONE_NUMBER_ID"))


def _base() -> str:
    return f"https://graph.facebook.com/{_cfg('WHATSAPP_API_VERSION', 'v25.0')}"


def _headers() -> dict:
    return {"Authorization": f"Bearer {_cfg('WHATSAPP_TOKEN')}"}


# --------------------------------------------------------------------------- #
# Inbound
# --------------------------------------------------------------------------- #
def verify_subscription(mode: str | None, token: str | None, challenge: str | None) -> str | None:
    expected = _cfg("WHATSAPP_VERIFY_TOKEN")
    if mode == "subscribe" and expected and token and hmac.compare_digest(token, expected) and challenge:
        return challenge
    return None


def valid_signature(raw_body: bytes, header: str | None) -> bool:
    secret = _cfg("WHATSAPP_APP_SECRET")
    if not secret:
        # Only for local development; production must set the app secret.
        return os.environ.get("MUNSHI_ALLOW_UNSIGNED") == "1"
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header[7:])


@dataclass
class WAMessage:
    wa_id: str
    message_id: str
    type: str                       # text | image | audio | interactive | button | other
    text: str = ""
    media_id: str | None = None
    mime_type: str | None = None
    profile_name: str | None = None
    timestamp: str | None = None


def parse_webhook(payload: dict) -> list[WAMessage]:
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                t = m.get("type")
                msg = WAMessage(wa_id=m.get("from"), message_id=m.get("id"), type=t or "other",
                                profile_name=names.get(m.get("from")), timestamp=m.get("timestamp"))
                if t == "text":
                    msg.text = (m.get("text") or {}).get("body", "")
                elif t in {"image", "audio", "document"}:
                    media = m.get(t) or {}
                    msg.media_id, msg.mime_type = media.get("id"), media.get("mime_type")
                    msg.text = media.get("caption", "") or ""
                    if t == "document" and not (msg.mime_type or "").startswith("image/"):
                        msg.type = "other"
                    elif t == "document":
                        msg.type = "image"
                elif t == "interactive":
                    i = m.get("interactive") or {}
                    reply = i.get("button_reply") or i.get("list_reply") or {}
                    msg.text = reply.get("id") or reply.get("title", "")
                elif t == "button":  # quick-reply button on a template
                    msg.text = (m.get("button") or {}).get("payload") or (m.get("button") or {}).get("text", "")
                else:
                    msg.type = "other"
                if msg.wa_id and msg.message_id:
                    out.append(msg)
    return out


def download_media(media_id: str) -> tuple[bytes, str]:
    with httpx.Client(timeout=60) as c:
        meta = c.get(f"{_base()}/{media_id}", headers=_headers())
        meta.raise_for_status()
        info = meta.json()
        blob = c.get(info["url"], headers=_headers(), follow_redirects=True)
        blob.raise_for_status()
        return blob.content, info.get("mime_type") or blob.headers.get("content-type", "application/octet-stream")


# --------------------------------------------------------------------------- #
# Outbound
# --------------------------------------------------------------------------- #
def _post(payload: dict) -> str:
    if not configured():
        raise DeliveryError("WhatsApp is not configured")
    try:
        r = httpx.post(f"{_base()}/{_cfg('WHATSAPP_PHONE_NUMBER_ID')}/messages", headers=_headers(),
                       json={"messaging_product": "whatsapp", **payload}, timeout=30)
    except httpx.HTTPError as e:
        raise DeliveryError(f"network error: {e}") from e
    if r.status_code >= 400:
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        code = err.get("code")
        detail = (err.get("error_data") or {}).get("details") or err.get("message") or r.text[:200]
        raise DeliveryError(f"WhatsApp error {code}: {detail}", code)
    return ((r.json().get("messages") or [{}])[0]).get("id", "")


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
    mid = ""
    for part in _chunks(text, TEXT_LIMIT):
        mid = _post({"to": to, "type": "text", "text": {"body": part, "preview_url": False}})
    return mid


def send_buttons(to: str, text: str, buttons: list[tuple[str, str]]) -> str:
    if len(text) > BUTTON_BODY_LIMIT:
        send_text(to, text)
        text = "Reply:"
    return _post({"to": to, "type": "interactive", "interactive": {
        "type": "button",
        "body": {"text": text},
        "action": {"buttons": [{"type": "reply", "reply": {"id": bid[:256], "title": title[:20]}}
                               for bid, title in buttons[:3]]},
    }})


def send_template(to: str, name: str, params: list[str]) -> str:
    clean = [re.sub(r"\s{2,}", " ", p.replace("\n", " · ").replace("\t", " "))[:1000] for p in params]
    comps = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in clean]}] if clean else []
    return _post({"to": to, "type": "template", "template": {
        "name": name, "language": {"code": _cfg("WHATSAPP_TEMPLATE_LANG", "en")}, "components": comps}})


def send_reply(to: str, reply) -> str:
    buttons = getattr(reply, "buttons", None)
    return send_buttons(to, str(reply), buttons) if buttons else send_text(to, str(reply))


def mark_read(message_id: str) -> None:
    try:
        _post({"status": "read", "message_id": message_id})
    except DeliveryError as e:
        log.debug("mark_read failed: %s", e)


class CloudTransport:
    """channels.Transport for messages Munshi starts (orders to distributors, notes to other shops)."""

    def send_order_to_distributor(self, to: str, shop_name: str, text: str) -> str:
        try:
            return send_text(to, text)
        except DeliveryError as e:
            tpl = _cfg("WHATSAPP_ORDER_TEMPLATE")
            if tpl and e.code in OUTSIDE_WINDOW_ERRORS:
                return send_template(to, tpl, [shop_name or "A Munshi shop", text])
            raise

    def send_text(self, to: str, text: str) -> str:
        return send_text(to, text)
