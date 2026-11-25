"""Speech-to-text for WhatsApp voice notes via Sarvam AI (Kannada, Hindi, code-mixed).

POST https://api.sarvam.ai/speech-to-text, model saaras:v4, mode "translit" so the transcript
comes back in Latin script ("Maggi khatam, Parle-G do bache"), which both the alias memory
and Claude normalise well. The shop's item names are passed as keyterms.
Env: SARVAM_API_KEY
"""
from __future__ import annotations

import json
import os

import httpx

URL = "https://api.sarvam.ai/speech-to-text"
LANG_CODES = {"kn": "kn-IN", "hi": "hi-IN", "en": "en-IN"}


class STTError(Exception):
    pass


def configured() -> bool:
    return bool(os.environ.get("SARVAM_API_KEY"))


def transcribe(audio: bytes, mime_type: str, language: str | None = None,
               keyterms: list[str] | None = None) -> str:
    if not configured():
        raise STTError("speech-to-text is not configured")
    ext = "ogg" if "ogg" in (mime_type or "") or "opus" in (mime_type or "") else (mime_type or "audio/ogg").split("/")[-1]
    data = {"model": "saaras:v4", "mode": "translit",
            "language_code": LANG_CODES.get(language or "", "unknown")}
    terms = [t[:64] for t in (keyterms or []) if t and t.strip()][:50]
    if terms:
        data["keyterms"] = json.dumps(terms, ensure_ascii=False)
    try:
        r = httpx.post(URL, headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
                       data=data, files={"file": (f"voice.{ext}", audio, (mime_type or "audio/ogg").split(";")[0])},
                       timeout=60)
    except httpx.HTTPError as e:
        raise STTError(f"network error: {e}") from e
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message") or r.text[:200]
        except ValueError:
            msg = r.text[:200]
        raise STTError(f"Sarvam {r.status_code}: {msg}")
    return (r.json().get("transcript") or "").strip()
