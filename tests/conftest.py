import os
from datetime import datetime

import pytest

os.environ.setdefault("MUNSHI_DB", ":memory:")
os.environ["MUNSHI_DISABLE_BACKGROUND"] = "1"

from src import channels, clock, db, llm  # noqa: E402

from .fixtures import build_shop, bill_extraction  # noqa: E402

NOW = datetime(2026, 11, 25, 10, 0)  # a Wednesday; salesman comes Thursday


class FakeTransport:
    def __init__(self, fail_code=None):
        self.sent, self.fail_code = [], fail_code

    def send_order_to_distributor(self, to, shop_name, text):
        if self.fail_code:
            raise channels.DeliveryError("outside 24h window", self.fail_code)
        self.sent.append((to, text))
        return "wamid.fake"

    def send_text(self, to, text):
        self.sent.append((to, text))
        return "wamid.fake"


@pytest.fixture()
def fake_llm(monkeypatch):
    """Claude reads the bill perfectly; name matching falls back to the offline matcher; no translation."""
    state = {"bill": None}
    monkeypatch.setenv("MUNSHI_LLM", "on")
    monkeypatch.setattr(llm, "extract_bill", lambda image, media_type, mismatch_note=None: state["bill"])

    def no_normalise(*a, **k):
        raise llm.LLMUnavailable("offline in tests")
    monkeypatch.setattr(llm, "normalise", no_normalise)
    monkeypatch.setattr(llm, "translate", lambda texts, lang: [f"[{lang}] {t}" for t in texts])
    return state


@pytest.fixture()
def transport():
    t = FakeTransport()
    channels.set_transport(t)
    yield t
    channels.set_transport(channels.NoTransport())


@pytest.fixture()
def conn(fake_llm, transport):
    clock.set_now(NOW)
    c = db.connect(":memory:")
    build_shop(c, NOW)
    fake_llm["bill"] = bill_extraction(NOW)
    yield c
    clock.set_now(None)


@pytest.fixture(autouse=True)
def _uploads_in_tmp(tmp_path, monkeypatch):
    from src import worker
    monkeypatch.setattr(worker, "UPLOADS", tmp_path / "uploads")
