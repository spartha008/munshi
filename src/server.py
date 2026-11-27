"""Munshi web service.

  GET  /                 product page with a WhatsApp link and QR code
  GET  /healthz          health check (Render)
  GET  /webhook          Meta webhook verification
  POST /webhook          inbound WhatsApp messages (signed by Meta)
  GET  /admin            operator console (HTTP Basic auth, ADMIN_PASSWORD) — not for shop owners

Run locally:  python -m uvicorn src.server:app --port 8000
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

import qrcode
import qrcode.image.svg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from . import channels, clock, db, insights, ledger, llm, scheduler, stt, twilio_wa, whatsapp
from .db import row, rows
from .tools import ordering
from .tools.stock import score_expiry
from .worker import UPLOADS, Worker

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("munshi")

WEB = Path(__file__).parent / "web"
_lock = threading.Lock()
_conn = db.connect()
db.init_db(_conn)
worker = Worker(_conn, _lock)


@asynccontextmanager
async def lifespan(app: FastAPI):
    transports: dict[str, channels.Transport] = {}
    if whatsapp.configured():
        transports["meta"] = whatsapp.CloudTransport()
    else:
        log.warning("Meta WhatsApp is not configured (WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID)")
    if twilio_wa.configured():
        transports["twilio"] = twilio_wa.Transport()
        log.info("Twilio WhatsApp channel on (%s)", "sandbox" if twilio_wa.is_sandbox() else "sender")
    if transports:
        channels.set_transport(channels.MultiTransport(transports))
    if not llm.enabled():
        log.warning("Claude is not configured (ANTHROPIC_API_KEY): bill photos can't be read")
    if not stt.configured():
        log.warning("Sarvam is not configured (SARVAM_API_KEY): voice notes will be declined politely")
    if os.environ.get("MUNSHI_DISABLE_BACKGROUND") != "1":
        worker.start()
        scheduler.start(worker)
    yield


app = FastAPI(title="Munshi", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=WEB), name="static")


# --------------------------------------------------------------------------- #
# Public
# --------------------------------------------------------------------------- #
def _page(name: str) -> str:
    """Serve an HTML page with static URLs stamped by file time, so a redeploy never runs stale JS/CSS."""
    html = (WEB / name).read_text(encoding="utf-8")
    return re.sub(r'/static/([\w.-]+\.(?:js|css))"',
                  lambda m: f'/static/{m.group(1)}?v={int((WEB / m.group(1)).stat().st_mtime)}"', html)


def _wa_number() -> tuple[str | None, str | None]:
    """The number and wa.me link the landing page shows. MUNSHI_PUBLIC_CHANNEL picks meta or twilio;
    by default Meta when configured, else the Twilio sandbox (whose link pre-fills its join code)."""
    public = os.environ.get("MUNSHI_PUBLIC_CHANNEL") or ("meta" if whatsapp.configured() else "twilio")
    if public == "twilio" and twilio_wa.configured():
        digits = re.sub(r"\D", "", os.environ.get("TWILIO_WHATSAPP_FROM") or "")
        join = os.environ.get("TWILIO_SANDBOX_JOIN")
        text = f"join%20{join}" if twilio_wa.is_sandbox() and join else "Hi%20Munshi"
        disp = f"+{digits}" + (f" (first message: join {join})" if twilio_wa.is_sandbox() and join else "")
        return (disp, f"https://wa.me/{digits}?text={text}") if digits else (None, None)
    disp = os.environ.get("WHATSAPP_DISPLAY_NUMBER")
    digits = re.sub(r"\D", "", disp or "")
    return (disp, f"https://wa.me/{digits}?text=Hi%20Munshi") if digits else (None, None)


@app.get("/", response_class=HTMLResponse)
def landing():
    html = _page("landing.html")
    disp, link = _wa_number()
    if link:
        html = html.replace("{{WA_LINK}}", link).replace("{{WA_NUMBER}}", disp).replace("{{HAS_NUMBER}}", "")
    else:
        html = html.replace("{{HAS_NUMBER}}", "hidden").replace("{{WA_LINK}}", "#").replace("{{WA_NUMBER}}", "")
    return html


@app.get("/qr.svg")
def qr():
    _, link = _wa_number()
    if not link:
        raise HTTPException(404)
    img = qrcode.make(link, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "max-age=3600"})


@app.get("/healthz")
def healthz():
    with _lock:
        _conn.execute("SELECT 1").fetchone()
    return {"ok": True, "whatsapp": whatsapp.configured(), "twilio": twilio_wa.configured(),
            "claude": llm.enabled(), "voice": stt.configured()}


# --------------------------------------------------------------------------- #
# WhatsApp webhook
# --------------------------------------------------------------------------- #
@app.get("/webhook")
def webhook_verify(request: Request):
    p = request.query_params
    challenge = whatsapp.verify_subscription(p.get("hub.mode"), p.get("hub.verify_token"), p.get("hub.challenge"))
    if challenge is None:
        raise HTTPException(403, "verification failed")
    return PlainTextResponse(challenge)


@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    if not whatsapp.valid_signature(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(403, "bad signature")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "invalid JSON")
    for msg in whatsapp.parse_webhook(payload):
        if worker.submit(msg) and os.environ.get("MUNSHI_SYNC") == "1":
            worker.process(worker.q.get_nowait())
    for st in whatsapp.parse_statuses(payload):
        if st["status"] == "failed":
            # Meta accepted the send but could not deliver it: keep the reason where the console shows it.
            log.warning("WhatsApp could not deliver %s to %s: %s", st["message_id"], st["recipient"], st["errors"])
            with _lock:
                _conn.execute("INSERT INTO trace (shop_id, message_id, tool, input, output, created_at) "
                              "VALUES (?,NULL,'whatsapp_delivery_failed',?,?,?)",
                              (st["recipient"] or "", json.dumps({"wa_message_id": st["message_id"]}),
                               json.dumps(st["errors"], ensure_ascii=False), db.iso(clock.now())))
        else:
            log.info("WhatsApp status: %s %s", st["status"], st["message_id"])
    return {"ok": True}


@app.post("/twilio")
async def twilio_webhook(request: Request):
    """Twilio posts URL-encoded form fields; the signature covers the public URL it was given plus those fields."""
    raw = await request.body()
    form = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
    url = os.environ.get("TWILIO_WEBHOOK_URL")
    if not url:  # rebuild the public URL behind a tunnel or proxy
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        url = f"{scheme}://{host}{request.url.path}" + (f"?{request.url.query}" if request.url.query else "")
    if not twilio_wa.valid_signature(url, form, request.headers.get("X-Twilio-Signature")):
        raise HTTPException(403, "bad signature")
    msg = twilio_wa.parse_form(form)
    if msg and worker.submit(msg) and os.environ.get("MUNSHI_SYNC") == "1":
        worker.process(worker.q.get_nowait())
    return Response("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>", media_type="application/xml")


# --------------------------------------------------------------------------- #
# Operator console (for the team, not shop owners)
# --------------------------------------------------------------------------- #
_basic = HTTPBasic(auto_error=False)


def admin(creds: HTTPBasicCredentials | None = Depends(_basic)):
    pw = os.environ.get("ADMIN_PASSWORD")
    if not pw:
        raise HTTPException(404)
    if not creds or not (secrets.compare_digest(creds.username, os.environ.get("ADMIN_USER", "admin"))
                         and secrets.compare_digest(creds.password, pw)):
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="Munshi admin"'})
    return True


def _num(x):
    return None if x is None or (isinstance(x, float) and (math.isinf(x) or math.isnan(x))) else round(x, 2)


@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(admin)])
def admin_page():
    return _page("admin.html")


@app.get("/admin/api/shops", dependencies=[Depends(admin)])
def admin_shops():
    with _lock:
        return {"now": clock.now().isoformat(), "status": {"whatsapp": whatsapp.configured(),
                "claude": llm.status(), "voice": stt.configured()},
                "shops": rows(_conn, """SELECT s.shop_id, s.name, s.locality, s.language, s.onboarding_step, s.channel,
                    s.salesman_day, s.cash_budget, s.created_at, s.last_inbound_at,
                    (SELECT COUNT(*) FROM message m WHERE m.shop_id=s.shop_id) AS messages,
                    (SELECT COUNT(*) FROM bill b WHERE b.shop_id=s.shop_id) AS bills,
                    (SELECT COUNT(*) FROM orders o WHERE o.shop_id=s.shop_id AND o.status='sent') AS orders_sent
                    FROM shop s ORDER BY COALESCE(s.last_inbound_at, s.created_at) DESC""")}


@app.get("/admin/api/shops/{shop_id}", dependencies=[Depends(admin)])
def admin_shop(shop_id: str):
    with _lock:
        c = _conn
        shop = row(c, "SELECT * FROM shop WHERE shop_id=?", shop_id)
        if not shop:
            raise HTTPException(404)
        if shop["onboarding_step"]:
            b = None
        else:
            bud = ordering.budget_for(c, shop_id)
            b = {"owner_cap": bud.owner_cap, "cash_on_hand": shop["cash_on_hand"],
                 "expected_udhaar": round(bud.expected_udhaar), "commitments": bud.commitments,
                 "available": round(bud.available), "cap": round(bud.cap)}
        states = ledger.all_states(c, shop_id)
        orders = rows(c, "SELECT * FROM orders WHERE shop_id=? ORDER BY order_id DESC LIMIT 5", shop_id)
        for o in orders:
            o["lines"] = ordering.order_lines(c, o["order_id"])
        return {
            "now": clock.now().isoformat(),
            "shop": shop,
            "distributor": row(c, "SELECT * FROM distributor WHERE distributor_id=?", shop["distributor_id"])
                           if shop["distributor_id"] else None,
            "budget": b,
            "kpis": insights.shop_kpis(c, shop_id),
            "messages": rows(c, "SELECT * FROM message WHERE shop_id=? ORDER BY id DESC LIMIT 200", shop_id)[::-1],
            "ledger": [{"sku_id": s.sku_id, "name": s.name, "category": s.category,
                        "velocity": _num(s.velocity), "band_low": _num(s.band.low), "band_high": _num(s.band.high),
                        "confidence": s.band.label, "source": s.source, "on_hand": _num(s.on_hand),
                        "cover_days": _num(s.cover_days), "censored": s.censored,
                        "rupees_at_risk": round(s.rupees_at_risk)} for s in states],
            "expiry": score_expiry(c, shop_id),
            "orders": orders,
            "outbox": rows(c, "SELECT * FROM outbox WHERE shop_id=? ORDER BY id DESC LIMIT 20", shop_id),
            "reminders": rows(c, "SELECT r.*, i.canonical_name AS name FROM reminder r LEFT JOIN item i "
                                 "ON i.sku_id=r.sku_id WHERE r.shop_id=? ORDER BY due_on", shop_id),
            "questions": rows(c, "SELECT * FROM question WHERE shop_id=? ORDER BY id DESC LIMIT 50", shop_id),
            "aliases": rows(c, "SELECT a.*, i.canonical_name AS name FROM alias a JOIN item i ON i.sku_id=a.sku_id "
                               "WHERE a.shop_id=? ORDER BY a.last_confirmed DESC", shop_id),
            "schemes": rows(c, "SELECT s.*, i.canonical_name AS name FROM scheme s JOIN item i ON i.sku_id=s.sku_id "
                               "WHERE s.shop_id=? ORDER BY scheme_id DESC LIMIT 20", shop_id),
            "udhaar": rows(c, "SELECT * FROM udhaar WHERE shop_id=? AND status='open' ORDER BY given_on", shop_id),
            "trace": rows(c, "SELECT * FROM trace WHERE shop_id=? ORDER BY id DESC LIMIT 150", shop_id),
        }


@app.get("/admin/api/demand-report", dependencies=[Depends(admin)])
def admin_demand_report():
    with _lock:
        return {"min_shops": insights.K_MIN_SHOPS, "rows": insights.demand_report(_conn)}


@app.get("/admin/demand-report.csv", dependencies=[Depends(admin)])
def admin_demand_csv():
    with _lock:
        data = insights.demand_report(_conn)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["locality", "category", "sku_id", "item", "shops", "next_week_units", "low", "high"])
    for r in data:
        w.writerow([r["locality"], r["category"], r["sku_id"], r["item"], r["shops"], r["next_week_units"],
                    r["low"], r["high"]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="munshi-demand-{clock.today()}.csv"'})


@app.get("/admin/media/uploads/{name}", dependencies=[Depends(admin)])
def admin_media(name: str):
    if not re.fullmatch(r"[0-9a-f]{32}\.(jpg|png|webp)", name):
        raise HTTPException(404)
    p = UPLOADS / name
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)
