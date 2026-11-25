"""Every tool call is written to the trace table so the demo can show the agent's work."""
from __future__ import annotations

import contextvars
import dataclasses
import functools
import json
import math

from . import clock
from .db import iso

_ctx: contextvars.ContextVar[tuple | None] = contextvars.ContextVar("munshi_trace", default=None)


def bind(conn, shop_id: str, message_id: int | None):
    return _ctx.set((conn, shop_id, message_id))


def unbind(token) -> None:
    _ctx.reset(token)


def _plain(x, depth=0):
    if depth > 4:
        return str(x)
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return {k: _plain(v, depth + 1) for k, v in dataclasses.asdict(x).items()}
    if isinstance(x, dict):
        return {str(k): _plain(v, depth + 1) for k, v in x.items() if k != "image"}
    if isinstance(x, (list, tuple)):
        return [_plain(v, depth + 1) for v in x[:20]]
    if isinstance(x, float):
        return None if math.isinf(x) or math.isnan(x) else round(x, 2)
    if isinstance(x, (bytes, bytearray)):
        return f"<{len(x)} bytes>"
    if hasattr(x, "execute"):  # a DB connection
        return None
    return x if isinstance(x, (int, str, bool, type(None))) else str(x)


def _short(obj, limit=1500) -> str:
    s = json.dumps(_plain(obj), ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def log(tool: str, inputs, output) -> None:
    ctx = _ctx.get()
    if not ctx:
        return
    conn, shop_id, message_id = ctx
    conn.execute("INSERT INTO trace (shop_id, message_id, tool, input, output, created_at) VALUES (?,?,?,?,?,?)",
                 (shop_id, message_id, tool, _short(inputs), _short(output), iso(clock.now())))


def traced(name: str):
    """Decorator: log the call's arguments (minus the DB connection) and its result."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            inputs = {"args": [a for a in args if not hasattr(a, "execute")], **kwargs}
            try:
                out = fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - logged, then re-raised
                log(name, inputs, {"error": f"{type(e).__name__}: {e}"})
                raise
            log(name, inputs, out)
            return out
        return inner
    return wrap
