"""Twilio WhatsApp channel: form parsing, signatures, button rendering, routing by shop channel."""
import base64
import hashlib
import hmac
import threading

import pytest
from fastapi.testclient import TestClient

from src import channels, twilio_wa
from src.agent import ORDER_BUTTONS, YES_NO, Reply
from src.onboarding import LANG_BUTTONS
from src.whatsapp import WAMessage
from src.worker import Worker

from .fixtures import SHOP


def form(wa="916300000001", sid="SM1", body="Hi", **extra):
    return {"MessageSid": sid, "From": f"whatsapp:+{wa}", "WaId": wa, "To": "whatsapp:+14155238886",
            "Body": body, "NumMedia": "0", "ProfileName": "Ravi", **extra}


def test_parse_text_image_audio():
    t = twilio_wa.parse_form(form(body="Maggi khatam"))
    assert (t.channel, t.type, t.text, t.wa_id, t.message_id) == ("twilio", "text", "Maggi khatam", "916300000001", "SM1")
    i = twilio_wa.parse_form(form(sid="SM2", NumMedia="1", MediaUrl0="https://api.twilio.com/m/1",
                                  MediaContentType0="image/jpeg", Body=""))
    assert i.type == "image" and i.media_id == "https://api.twilio.com/m/1"
    a = twilio_wa.parse_form(form(sid="SM3", NumMedia="1", MediaUrl0="https://api.twilio.com/m/2",
                                  MediaContentType0="audio/ogg", Body=""))
    assert a.type == "audio"
    assert twilio_wa.parse_form({"Body": "x"}) is None


def sign(url, params, token="tok"):
    payload = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def test_signature(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    url, p = "https://x.trycloudflare.com/twilio", form()
    assert twilio_wa.valid_signature(url, p, sign(url, p))
    assert not twilio_wa.valid_signature(url, p, sign(url, p, "other"))
    assert not twilio_wa.valid_signature(url, p, None)


def test_buttons_become_reply_hints():
    assert twilio_wa.buttons_as_text(LANG_BUTTONS) == "Reply 1 for English, 2 for ಕನ್ನಡ, 3 for हिंदी."
    assert twilio_wa.buttons_as_text(ORDER_BUTTONS) == "Reply OK or Edit."
    assert twilio_wa.buttons_as_text(YES_NO) == "Reply Yes or No."


def test_sandbox_refuses_distributor_sends(monkeypatch):
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    with pytest.raises(channels.DeliveryError) as e:
        twilio_wa.Transport().send_order_to_distributor("919000000099", "Shop", "order")
    assert e.value.code == 63016


def test_multi_transport_routes_by_channel():
    class T:
        def __init__(self): self.calls = []
        def send_order_to_distributor(self, to, shop_name, text, channel=None): self.calls.append(("order", channel)); return "x"
        def send_text(self, to, text, channel=None): self.calls.append(("text", channel)); return "x"
    m, t = T(), T()
    mt = channels.MultiTransport({"meta": m, "twilio": t})
    mt.send_text("1", "a", channel="twilio"); mt.send_order_to_distributor("1", "s", "o", channel="meta")
    mt.send_text("1", "a", channel=None)  # unknown -> meta
    assert t.calls == [("text", "twilio")] and [c[0] for c in m.calls] == ["order", "text"]


def test_worker_replies_on_the_channel_the_message_came_from(conn):
    sent = {"meta": [], "twilio": []}
    from src.worker import ChannelOps
    ops = {ch: ChannelOps(send=lambda to, r, ch=ch: sent[ch].append(str(r)) or "id",
                          download=lambda mid: (b"", "image/jpeg"), mark_read=lambda mid: None) for ch in sent}
    w = Worker(conn, threading.Lock(), ops=ops)
    w.process(WAMessage("916300000002", "t1", "text", text="Hi", profile_name="New", channel="twilio"))
    assert sent["twilio"] and not sent["meta"]
    assert conn.execute("SELECT channel FROM shop WHERE shop_id='916300000002'").fetchone()[0] == "twilio"
    # the existing (meta) shop keeps getting meta replies
    w.process(WAMessage(SHOP, "t2", "text", text="settings"))
    assert sent["meta"] and len(sent["twilio"]) == 1


def test_distributor_order_uses_the_shops_channel(conn, transport):
    conn.execute("UPDATE shop SET channel='twilio' WHERE shop_id=?", (SHOP,))
    from src import agent
    agent.handle(conn, SHOP, agent.Inbound("text", text="order"))
    agent.handle(conn, SHOP, agent.Inbound("text", text="ok"))
    assert transport.channel == "twilio"


@pytest.fixture()
def client(monkeypatch, fake_llm):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    monkeypatch.setenv("TWILIO_SANDBOX_JOIN", "silver-fox")
    monkeypatch.setenv("MUNSHI_PUBLIC_CHANNEL", "twilio")
    monkeypatch.setenv("MUNSHI_SYNC", "1")
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    from src import server
    sent = []
    monkeypatch.setitem(server.worker.ops, "twilio",
                        type(server.worker.ops["twilio"])(send=lambda to, r: sent.append((to, str(r))) or "SM",
                                                         download=lambda u: (b"", "image/jpeg"),
                                                         mark_read=lambda m: None))
    with TestClient(server.app) as c:
        c.sent = sent
        yield c


def test_twilio_webhook_signed_through_a_tunnel(client):
    public = "https://x.trycloudflare.com/twilio"
    p = form(wa="916300000003", sid="SM9")
    r = client.post("/twilio", data=p, headers={"X-Twilio-Signature": sign(public, p),
                                                "X-Forwarded-Proto": "https", "X-Forwarded-Host": "x.trycloudflare.com"})
    assert r.status_code == 200 and "<Response>" in r.text
    assert client.sent and "Which language" in client.sent[-1][1]
    assert client.post("/twilio", data=p, headers={"X-Twilio-Signature": "bad"}).status_code == 403


def test_landing_page_offers_the_sandbox_join_link(client):
    page = client.get("/").text
    assert "https://wa.me/14155238886?text=join%20silver-fox" in page
    assert client.get("/healthz").json()["twilio"] is True
