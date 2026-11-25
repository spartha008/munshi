"""Claude is used for reading (bill photos, owner phrases), never for the arithmetic.

Runs only when credentials exist (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an
`ant auth login` profile). Set MUNSHI_LLM=off to force the offline path.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import anthropic

from .db import ROOT

MODEL = os.environ.get("MUNSHI_MODEL", "claude-opus-5-5")
EFFORT_BILL = os.environ.get("MUNSHI_EFFORT_BILL", "medium")
EFFORT_NAMES = os.environ.get("MUNSHI_EFFORT_NAMES", "low")
PROMPTS = ROOT / "prompts"

_client: anthropic.Anthropic | None = None
_disabled_reason: str | None = None


class LLMUnavailable(Exception):
    pass


class LLMRefused(Exception):
    pass


def enabled() -> bool:
    mode = os.environ.get("MUNSHI_LLM", "auto").lower()
    if mode == "off" or _disabled_reason:
        return False
    if mode == "on":
        return True
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("ANTHROPIC_PROFILE")
                or (Path.home() / ".config" / "anthropic").exists())


def status() -> dict:
    return {"enabled": enabled(), "model": MODEL if enabled() else None, "disabled_reason": _disabled_reason}


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(timeout=180.0, max_retries=2)
    return _client


def _nullable(t: str) -> dict:
    return {"anyOf": [{"type": t}, {"type": "null"}]}


BILL_SCHEMA = {
    "type": "object",
    "properties": {
        "distributor": _nullable("string"),
        "bill_date": _nullable("string"),
        "bill_total": _nullable("number"),
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": "number"},
                    "unit": _nullable("string"),
                    "unit_price": _nullable("number"),
                    "mrp": _nullable("number"),
                    "line_total": _nullable("number"),
                    "batch": _nullable("string"),
                    "expiry": _nullable("string"),
                    "scheme": _nullable("string"),
                    "confidence": {"type": "number"},
                },
                "required": ["name", "qty", "unit", "unit_price", "mrp", "line_total", "batch", "expiry",
                             "scheme", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["distributor", "bill_date", "bill_total", "items"],
    "additionalProperties": False,
}

NAME_SCHEMA = {
    "type": "object",
    "properties": {
        "sku_id": _nullable("string"),
        "sku_name": _nullable("string"),
        "qty": _nullable("number"),
        "action": {"type": "string", "enum": ["sold_out", "count", "running_low", "unknown"]},
        "confidence": {"type": "number"},
        "question": _nullable("string"),
    },
    "required": ["sku_id", "sku_name", "qty", "action", "confidence", "question"],
    "additionalProperties": False,
}


def _call_json(content: list[dict], schema: dict, effort: str) -> dict:
    """One Messages call that must return JSON matching `schema`."""
    global _disabled_reason
    try:
        resp = _get_client().beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",   # a policy decline is retried server-side on a recommended model
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.AuthenticationError as e:
        _disabled_reason = "authentication failed: check ANTHROPIC_API_KEY"
        raise LLMUnavailable(_disabled_reason) from e
    except anthropic.PermissionDeniedError as e:
        raise LLMUnavailable("API key lacks permission for this model") from e
    except anthropic.RateLimitError as e:
        raise LLMUnavailable("rate limited, try again in a minute") from e
    except anthropic.APIStatusError as e:
        raise LLMUnavailable(f"API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMUnavailable("network error reaching the Claude API") from e

    if resp.stop_reason == "refusal":
        category = getattr(resp.stop_details, "category", None) if resp.stop_details else None
        raise LLMRefused(f"declined ({category})")
    if resp.stop_reason == "max_tokens":
        raise LLMUnavailable("response was cut off")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise LLMUnavailable("empty response")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMUnavailable("response was not valid JSON") from e


def extract_bill(image: bytes, media_type: str, mismatch_note: str | None = None) -> dict:
    prompt = (PROMPTS / "bill_extraction.md").read_text(encoding="utf-8")
    if mismatch_note:
        prompt += f"\n\nA previous read of this bill did not balance: {mismatch_note} Re-read every line."
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                     "data": base64.standard_b64encode(image).decode("ascii")}},
        {"type": "text", "text": prompt},
    ]
    return _call_json(content, BILL_SCHEMA, EFFORT_BILL)


TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {"translations": {"type": "array", "items": {"type": "string"}}},
    "required": ["translations"],
    "additionalProperties": False,
}
LANG_NAMES = {"kn": "Kannada", "hi": "Hindi"}


def translate(texts: list[str], lang: str) -> list[str]:
    """Owner-facing replies in Kannada or Hindi. Numbers, amounts and commands stay exactly as written."""
    if lang not in LANG_NAMES or not texts:
        return texts
    prompt = (
        f"Translate each WhatsApp message below into simple, everyday spoken {LANG_NAMES[lang]}, the way a "
        "shop assistant talks to a kirana owner. Use the native script. Keep these exactly as written: every "
        "number, rupee amount (₹), date, product and brand name, and anything in double quotes (they are "
        "commands the owner types, like \"order\" or \"maggi 36\"). Keep line breaks and bullet points. "
        "Return the translations in the same order.\n\n"
        + "\n".join(f'<message index="{i}">\n{t}\n</message>' for i, t in enumerate(texts))
    )
    out = _call_json([{"type": "text", "text": prompt}], TRANSLATE_SCHEMA, "low").get("translations") or []
    return out if len(out) == len(texts) else texts


def normalise(phrase: str, aliases: list[tuple[str, str]], catalogue: list[tuple[str, str, str]]) -> dict:
    template = (PROMPTS / "name_normalisation.md").read_text(encoding="utf-8")
    alias_txt = "\n".join(f'"{p}" -> {s}' for p, s in aliases) or "(none yet)"
    cat_txt = "\n".join(f"{s}, {n}, {pk}" for s, n, pk in catalogue)
    prompt = (template.replace("{aliases}", alias_txt).replace("{catalogue}", cat_txt)
              .replace("{phrase}", phrase.replace('"', "'")))
    return _call_json([{"type": "text", "text": prompt}], NAME_SCHEMA, EFFORT_NAMES)
