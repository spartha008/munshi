"""Processes inbound WhatsApp messages one at a time, off the webhook request.

Meta expects a fast 200 and retries otherwise, so the webhook only verifies, de-duplicates
and queues; this worker downloads media, transcribes voice, runs the agent, translates if the
owner chose Kannada or Hindi, and sends the replies.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Callable

from . import agent, clock, llm, onboarding, stt, whatsapp
from .agent import Reply
from .db import DB_PATH, iso, parse_dt, row, rows

log = logging.getLogger("munshi.worker")
UPLOADS = Path(DB_PATH).parent / "uploads"

UNSUPPORTED = ("I can read distributor bill photos, voice notes and text messages. "
               "Please send one of those.")


def _keyterms(conn, shop_id: str) -> list[str]:
    phrases = [a["phrase"] for a in rows(conn, "SELECT phrase FROM alias WHERE shop_id=? ORDER BY last_confirmed DESC "
                                               "LIMIT 30", shop_id)]
    names = [i["canonical_name"] for i in rows(conn, """SELECT DISTINCT i.canonical_name FROM item i JOIN purchase p
                                                          ON p.sku_id=i.sku_id WHERE p.shop_id=? LIMIT 30""", shop_id)]
    seen, out = set(), []
    for t in names + phrases:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:50]


def localise(replies: list[Reply], lang: str | None) -> list[Reply]:
    if lang not in llm.LANG_NAMES or not replies or not llm.enabled():
        return replies
    try:
        out = llm.translate([str(r) for r in replies], lang)
        return [Reply(t, r.buttons) for t, r in zip(out, replies)]
    except (llm.LLMUnavailable, llm.LLMRefused) as e:
        log.warning("translation failed, sending English: %s", e)
        return replies


class Worker:
    def __init__(self, conn, lock: threading.Lock, send: Callable[[str, Reply], str] = whatsapp.send_reply,
                 download: Callable[[str], tuple[bytes, str]] = whatsapp.download_media,
                 transcribe: Callable[..., str] = stt.transcribe, mark_read: Callable[[str], None] = whatsapp.mark_read):
        self.conn, self.lock = conn, lock
        self.send, self.download, self.transcribe, self.mark_read = send, download, transcribe, mark_read
        self.q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    # -- queueing ------------------------------------------------------------
    def start(self) -> None:
        if not self._thread:
            self._thread = threading.Thread(target=self._loop, name="munshi-worker", daemon=True)
            self._thread.start()

    def submit(self, msg: whatsapp.WAMessage) -> bool:
        """De-duplicate (Meta retries) and queue. Returns False for a repeat delivery."""
        with self.lock:
            cur = self.conn.execute("INSERT OR IGNORE INTO wa_inbound VALUES (?,?)", (msg.message_id, iso(clock.now())))
        if cur.rowcount == 0:
            return False
        self.q.put(msg)
        return True

    def _loop(self) -> None:
        while True:
            msg = self.q.get()
            try:
                self.process(msg)
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("failed to process message %s", msg.message_id)
            finally:
                self.q.task_done()

    # -- processing ------------------------------------------------------------
    def process(self, msg: whatsapp.WAMessage) -> list[Reply]:
        self.mark_read(msg.message_id)
        with self.lock:
            shop = row(self.conn, "SELECT * FROM shop WHERE shop_id=?", msg.wa_id)
            if shop:
                self.conn.execute("UPDATE shop SET last_inbound_at=? WHERE shop_id=?", (iso(clock.now()), msg.wa_id))
        if not shop:
            with self.lock:
                replies = onboarding.start(self.conn, msg.wa_id, msg.profile_name)
                agent.log_message(self.conn, msg.wa_id, "in", "text", msg.text or f"[{msg.type}]")
                for r in replies:
                    agent.log_message(self.conn, msg.wa_id, "out", "text", str(r))
            return self._deliver(msg.wa_id, replies, "en")

        if shop["onboarding_step"]:
            text = msg.text if msg.type in {"text", "interactive", "button"} else ""
            with self.lock:
                replies = onboarding.handle(self.conn, shop, text) if text else [
                    Reply("Let's finish setting up first — then send me your bills.")]
                lang = row(self.conn, "SELECT language FROM shop WHERE shop_id=?", msg.wa_id)["language"]
            return self._deliver(msg.wa_id, replies, lang)

        inbound = self._to_inbound(msg, shop)
        if isinstance(inbound, Reply):
            return self._deliver(msg.wa_id, [inbound], shop["language"])
        with self.lock:
            replies = agent.handle(self.conn, msg.wa_id, inbound)
        return self._deliver(msg.wa_id, replies, shop["language"])

    def _to_inbound(self, msg: whatsapp.WAMessage, shop: dict) -> agent.Inbound | Reply:
        if msg.type in {"text", "interactive", "button"}:
            return agent.Inbound("text", text=msg.text, wa_message_id=msg.message_id)
        if msg.type == "image":
            try:
                data, mime = self.download(msg.media_id)
            except Exception as e:  # noqa: BLE001
                log.warning("media download failed: %s", e)
                return Reply("I couldn't download that photo. Please send it again.")
            UPLOADS.mkdir(parents=True, exist_ok=True)
            ext = {"image/png": ".png", "image/webp": ".webp"}.get(mime, ".jpg")
            name = f"{uuid.uuid4().hex}{ext}"
            (UPLOADS / name).write_bytes(data)
            return agent.Inbound("image", text=msg.text, image=data, media_type=mime.split(";")[0],
                                 media_path=f"uploads/{name}", wa_message_id=msg.message_id)
        if msg.type == "audio":
            if not stt.configured():
                return Reply("I can't listen to voice notes yet. Please type it, e.g. \"Maggi khatam, Parle-G do bache\".")
            try:
                data, mime = self.download(msg.media_id)
                with self.lock:
                    terms = _keyterms(self.conn, shop["shop_id"])
                text = self.transcribe(data, mime, shop["language"], terms)
            except Exception as e:  # noqa: BLE001
                log.warning("voice note failed: %s", e)
                return Reply("I couldn't hear that voice note clearly. Please try again, or type it.")
            if not text:
                return Reply("That voice note was empty. Please try again.")
            return agent.Inbound("voice", text=text, wa_message_id=msg.message_id)
        return Reply(UNSUPPORTED)

    def _deliver(self, to: str, replies: list[Reply], lang: str | None) -> list[Reply]:
        out = localise(replies, lang)
        for r in out:
            try:
                self.send(to, r)
            except Exception as e:  # noqa: BLE001
                log.error("send to %s failed: %s", to, e)
        return out

    # -- proactive (scheduler) ---------------------------------------------------
    def can_message_freely(self, shop: dict) -> bool:
        """WhatsApp allows free-form business messages only within 24 hours of the owner's last message."""
        last = shop.get("last_inbound_at")
        return bool(last) and clock.now() - parse_dt(last) < timedelta(hours=23, minutes=30)

    def proactive(self, shop: dict, replies: list[Reply]) -> bool:
        if not replies:
            return False
        if self.can_message_freely(shop):
            self._deliver(shop["shop_id"], replies, shop["language"])
            return True
        tpl = os.environ.get("WHATSAPP_NUDGE_TEMPLATE")
        if tpl:
            try:
                whatsapp.send_template(shop["shop_id"], tpl, [shop["name"] or "your shop"])
                return True
            except Exception as e:  # noqa: BLE001
                log.error("nudge template to %s failed: %s", shop["shop_id"], e)
        log.info("skipped proactive message to %s: outside the 24-hour window", shop["shop_id"])
        return False
