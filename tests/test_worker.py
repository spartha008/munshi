"""Worker: WhatsApp media -> agent, voice notes via speech-to-text, translation, de-duplication."""
import threading

from src import stt
from src.whatsapp import WAMessage
from src.worker import Worker

from .fixtures import SHOP


def make(conn, transcript="Maggi khatam, Parle-G do bache"):
    sent, calls = [], {}

    def transcribe(audio, mime, lang, terms):
        calls["stt"] = (mime, lang, terms)
        return transcript
    w = Worker(conn, threading.Lock(), send=lambda to, r: sent.append(str(r)) or "id",
               download=lambda mid: (b"bytes", "image/jpeg" if mid == "IMG" else "audio/ogg; codecs=opus"),
               transcribe=transcribe, mark_read=lambda mid: None)
    return w, sent, calls


def test_bill_photo_from_whatsapp(conn):
    w, sent, _ = make(conn)
    w.process(WAMessage(SHOP, "a1", "image", media_id="IMG"))
    assert sent[0].startswith("Got it: 14 items")
    path = conn.execute("SELECT media_path FROM message WHERE kind='image'").fetchone()[0]
    assert path.startswith("uploads/")


def test_voice_note_is_transcribed_with_shop_keyterms(conn, monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "k")
    w, sent, calls = make(conn)
    w.process(WAMessage(SHOP, "a2", "audio", media_id="AUD"))
    assert "Maggi 2-Minute Noodles 70g: sold out" in sent[0]
    mime, lang, terms = calls["stt"]
    assert mime.startswith("audio/ogg") and lang == "en" and "Parle-G ₹5 pack" in terms
    assert conn.execute("SELECT kind, body FROM message WHERE kind='voice'").fetchone()[1].startswith("Maggi")


def test_voice_without_sarvam_asks_to_type(conn, monkeypatch):
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    assert not stt.configured()
    w, sent, _ = make(conn)
    w.process(WAMessage(SHOP, "a3", "audio", media_id="AUD"))
    assert "Please type it" in sent[0]


def test_replies_are_translated_for_kannada_owners(conn):
    conn.execute("UPDATE shop SET language='kn' WHERE shop_id=?", (SHOP,))
    w, sent, _ = make(conn)
    w.process(WAMessage(SHOP, "a4", "text", text="settings"))
    assert sent[0].startswith("[kn] ")


def test_submit_deduplicates_meta_retries(conn):
    w, _, _ = make(conn)
    m = WAMessage(SHOP, "dup1", "text", text="hi")
    assert w.submit(m) is True
    assert w.submit(m) is False


def test_greeting_hi_does_not_pick_hindi_during_onboarding(conn):
    w, sent, _ = make(conn)
    w.process(WAMessage("919333300001", "n1", "text", text="Hi", profile_name="New"))
    w.process(WAMessage("919333300001", "n2", "text", text="hi"))
    assert "Please pick a language" in sent[-1]
    shop = conn.execute("SELECT language, onboarding_step FROM shop WHERE shop_id='919333300001'").fetchone()
    assert tuple(shop) == ("en", "language")
    w.process(WAMessage("919333300001", "n3", "text", text="hindi"))
    assert conn.execute("SELECT language FROM shop WHERE shop_id='919333300001'").fetchone()[0] == "hi"
