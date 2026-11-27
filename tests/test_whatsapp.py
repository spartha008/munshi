"""WhatsApp Cloud API layer and the HTTP server, end to end with signed webhooks."""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from src import whatsapp


def payload(wa_id, msg_id, kind="text", body="Hi", name="Ravi", **extra):
    m = {"from": wa_id, "id": msg_id, "timestamp": "1764045000", "type": kind}
    if kind == "text":
        m["text"] = {"body": body}
    elif kind == "interactive":
        m["interactive"] = {"type": "button_reply", "button_reply": {"id": body, "title": body.title()}}
    else:
        m[kind] = extra
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "123"},
        "contacts": [{"wa_id": wa_id, "profile": {"name": name}}], "messages": [m]}}]}]}


def test_parse_text_image_audio_and_button():
    assert whatsapp.parse_webhook(payload("91999", "w1", body="Maggi khatam"))[0].text == "Maggi khatam"
    img = whatsapp.parse_webhook(payload("91999", "w2", "image", id="MEDIA1", mime_type="image/jpeg", caption="bill"))[0]
    assert (img.type, img.media_id, img.text) == ("image", "MEDIA1", "bill")
    aud = whatsapp.parse_webhook(payload("91999", "w3", "audio", id="MEDIA2", mime_type="audio/ogg; codecs=opus"))[0]
    assert (aud.type, aud.media_id) == ("audio", "MEDIA2")
    btn = whatsapp.parse_webhook(payload("91999", "w4", "interactive", body="ok"))[0]
    assert btn.text == "ok" and btn.profile_name == "Ravi"
    status_only = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "read"}]}}]}]}
    assert whatsapp.parse_webhook(status_only) == []


def test_signature_and_subscription(monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "s3cret")
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert whatsapp.valid_signature(body, good)
    assert not whatsapp.valid_signature(body, "sha256=" + "0" * 64)
    assert not whatsapp.valid_signature(body, None)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "vt")
    assert whatsapp.verify_subscription("subscribe", "vt", "42") == "42"
    assert whatsapp.verify_subscription("subscribe", "nope", "42") is None


def test_unsigned_webhooks_rejected_without_secret(monkeypatch):
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    monkeypatch.delenv("MUNSHI_ALLOW_UNSIGNED", raising=False)
    assert not whatsapp.valid_signature(b"{}", None)


@pytest.fixture()
def client(monkeypatch, fake_llm):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "s3cret")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "vt")
    monkeypatch.setenv("MUNSHI_SYNC", "1")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("WHATSAPP_DISPLAY_NUMBER", "+1 555 010 0200")
    from src import server
    from src.worker import ChannelOps
    sent = []
    monkeypatch.setitem(server.worker.ops, "meta", ChannelOps(
        send=lambda to, r: sent.append((to, str(r), r.buttons)) or "wamid",
        download=lambda mid: (b"", "image/jpeg"), mark_read=lambda mid: None))
    with TestClient(server.app) as c:
        c.sent = sent
        yield c


def post(client, p):
    raw = json.dumps(p).encode()
    sig = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": sig,
                                                         "Content-Type": "application/json"})


def test_webhook_verification(client):
    r = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "777"})
    assert r.status_code == 200 and r.text == "777"
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "x",
                                          "hub.challenge": "1"}).status_code == 403


def test_bad_signature_is_rejected(client):
    r = client.post("/webhook", content=b"{}", headers={"X-Hub-Signature-256": "sha256=00"})
    assert r.status_code == 403


def test_onboarding_over_whatsapp_then_commands(client):
    wa = "918888800001"
    steps = [("interactive", "lang_kn"), ("text", "Sri Ganesh Stores"), ("text", "Saraswathipuram, Mysuru"),
             ("text", "Gupta Agencies 9876543210"), ("text", "friday"), ("text", "6000")]
    assert post(client, payload(wa, "m0", body="Hi")).status_code == 200
    first = client.sent[-1]
    assert "Which language" in first[1] and [b[0] for b in first[2]] == ["lang_en", "lang_kn", "lang_hi"]
    for i, (kind, body) in enumerate(steps, 1):
        post(client, payload(wa, f"m{i}", kind, body=body))
    assert any("All set, Sri Ganesh Stores" in s[1] for s in client.sent)
    assert client.sent[-1][1].startswith("[kn] ")  # replies are translated for a Kannada owner

    from src import server
    from src.db import row
    shop = row(server._conn, "SELECT * FROM shop WHERE shop_id=?", wa)
    assert shop["onboarding_step"] is None and shop["salesman_day"] == "Friday" and shop["cash_budget"] == 6000
    dist = row(server._conn, "SELECT * FROM distributor WHERE distributor_id=?", shop["distributor_id"])
    assert (dist["name"], dist["phone"]) == ("Gupta Agencies", "919876543210")

    n = len(client.sent)
    post(client, payload(wa, "m9", body="udhaar Ramesh 500"))
    post(client, payload(wa, "m9", body="udhaar Ramesh 500"))  # Meta retry: same id, processed once
    assert len(client.sent) == n + 1 and "Ramesh now owes ₹500" in client.sent[-1][1]


def test_public_pages_and_admin_auth(client):
    assert client.get("/healthz").json()["ok"] is True
    page = client.get("/").text
    assert "https://wa.me/15550100200" in page and "{{" not in page
    assert client.get("/qr.svg").headers["content-type"].startswith("image/svg")
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", auth=("admin", "wrong")).status_code == 401
    assert client.get("/admin", auth=("admin", "pw")).status_code == 200
    shops = client.get("/admin/api/shops", auth=("admin", "pw")).json()["shops"]
    assert isinstance(shops, list)
    assert client.get("/admin/media/uploads/..%2F..%2Fmunshi.db", auth=("admin", "pw")).status_code == 404


def test_failed_delivery_status_is_recorded(client):
    p = {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.X", "status": "failed", "recipient_id": "919000000001",
         "errors": [{"code": 131026, "title": "Message undeliverable", "error_data": {"details": "not on WhatsApp"}}]}]}}]}]}
    assert post(client, p).status_code == 200
    from src import server
    row_ = server._conn.execute("SELECT shop_id, output FROM trace WHERE tool='whatsapp_delivery_failed'").fetchone()
    assert row_[0] == "919000000001" and "131026" in row_[1]
