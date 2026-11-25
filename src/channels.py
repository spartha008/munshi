"""Outbound transport used for messages that are not replies to the owner (e.g. the order to
the distributor). The server installs the WhatsApp transport; tests install a fake.
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
    def send_order_to_distributor(self, to: str, shop_name: str, text: str) -> str:
        """Returns a provider message id, or raises DeliveryError."""

    def send_text(self, to: str, text: str) -> str:
        """A business-initiated text to another number. Raises DeliveryError."""


class NoTransport:
    def send_order_to_distributor(self, to: str, shop_name: str, text: str) -> str:
        raise DeliveryError("no outbound channel configured")

    def send_text(self, to: str, text: str) -> str:
        raise DeliveryError("no outbound channel configured")


_transport: Transport = NoTransport()


def set_transport(t: Transport) -> None:
    global _transport
    _transport = t


def get_transport() -> Transport:
    return _transport
