"""Which model each call goes to, and what the request carries. No network: the client is faked."""
from types import SimpleNamespace

import pytest

from src import llm


@pytest.fixture()
def calls(monkeypatch):
    """Capture every Messages request; answer each with a small valid JSON object."""
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(stop_reason="end_turn", stop_details=None,
                               content=[SimpleNamespace(type="text", text='{"translations": ["x"]}')])
    fake = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_get_client", lambda: fake)
    return seen


def test_bills_go_to_opus_with_server_side_fallback(calls):
    llm.extract_bill(b"\x89PNG", "image/png")
    req = calls[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in req["betas"]
    assert req["messages"][0]["content"][0]["type"] == "image"
    assert req["output_config"]["format"]["type"] == "json_schema"


def test_names_and_translation_go_to_haiku_without_fallback(calls):
    llm.normalise("maggi khatam", [("maggi", "MAG70")], [("MAG70", "Maggi 70g", "70 g")])
    llm.translate(["Order ready"], "kn")
    assert [c["model"] for c in calls] == ["claude-haiku-5-5", "claude-haiku-5-5"]
    assert all("fallbacks" not in c and "betas" not in c for c in calls)
    assert calls[0]["output_config"]["effort"] == "low"


def test_text_model_can_be_changed_and_gets_fallback_when_not_haiku(calls, monkeypatch):
    monkeypatch.setattr(llm, "MODEL_TEXT", "claude-sonnet-5-5")
    llm.normalise("parle", [], [])
    assert calls[0]["model"] == "claude-sonnet-5-5" and calls[0]["fallbacks"] == "default"


def test_status_reports_both_models(monkeypatch):
    monkeypatch.setenv("MUNSHI_LLM", "on")
    s = llm.status()
    assert s["enabled"] and s["model"] == "claude-opus-5-5" and s["text_model"] == "claude-haiku-5-5"
