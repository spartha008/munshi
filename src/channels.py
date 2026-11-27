"""Outbound transport for messages Munshi starts itself (the order to the distributor, a note to
another shop). Replies to an owner go through the worker instead.

Two channels can be live at once: Meta's Cloud API and Twilio. A message to someone goes out on
the channel of the shop it concerns, so the recipient sees it from the number they already know.
"""
from __future__ import annotations

from typing import Protocol

# WhatsApp error codes meaning "outside the 24-hour customer-service window"
OUTSIDE_WINDOW_ERRORS = {131047, 470}


class DeliveryError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class Transport(Protocol):
    def send_order_to_distributor(self, to: str, shop_name: str, text: str, channel: str | None = None) -> str:
        """Returns a provider message id, or raises DeliveryError."""

    def send_text(self, to: str, text: str, channel: str | None = None) -> str:
        """A business-initiated text to another number. Raises DeliveryError."""


class NoTransport:
    def send_order_to_distributor(self, to: str, shop_name: str, text: str, channel: str | None = None) -> str:
        raise DeliveryError("no outbound channel configured")

    def send_text(self, to: str, text: str, channel: str | None = None) -> str:
        raise DeliveryError("no outbound channel configured")


class MultiTransport:
    """Routes by channel name; falls back to whichever single channel exists."""

    def __init__(self, by_channel: dict[str, Transport]):
        self.by_channel = by_channel

    def _pick(self, channel: str | None) -> Transport:
        if channel in self.by_channel:
            return self.by_channel[channel]
        if len(self.by_channel) == 1:
            return next(iter(self.by_channel.values()))
        if "meta" in self.by_channel:
            return self.by_channel["meta"]
        raise DeliveryError(f"no outbound channel for '{channel}'")

    def send_order_to_distributor(self, to: str, shop_name: str, text: str, channel: str | None = None) -> str:
        return self._pick(channel).send_order_to_distributor(to, shop_name, text, channel)

    def send_text(self, to: str, text: str, channel: str | None = None) -> str:
        return self._pick(channel).send_text(to, text, channel)


_transport: Transport = NoTransport()


def set_transport(t: Transport) -> None:
    global _transport
    _transport = t


def get_transport() -> Transport:
    return _transport
