"""Delivery router — how the runtime sends outbound messages, interface-agnostically.

Phase W (Interface Intelligence) requires that the runtime — not WhatsApp —
understands delivery. The DeliveryRouter is the runtime's single outbound
door: interface adapters register a sender callable, and runtime mechanisms
(capabilities, background work) deliver through the router without knowing
which interface is attached.

The router knows NOTHING about WhatsApp. It knows about interface kinds,
recipients, and text. Adding Telegram/web means registering another sender.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from wax.core.exceptions import WaxNotFoundError
from wax.runtime.logging import get_logger

log = get_logger(__name__)

# A sender is an async callable (recipient_interface_id, text) -> provider ack dict.
SenderFn = Callable[[str, str], Awaitable[dict[str, Any]]]


class SendResult(dict[str, Any]):
    """Type alias stand-in; kept trivial on purpose."""


class DeliveryRouter:
    """Registry of interface senders + the runtime's outbound send API."""

    def __init__(self) -> None:
        self._senders: dict[str, SenderFn] = {}

    def register(self, interface_kind: str, sender: SenderFn) -> None:
        """Attach an interface sender. Re-registering replaces silently."""
        self._senders[interface_kind] = sender
        log.info("delivery.interface_registered", interface=interface_kind)

    def has(self, interface_kind: str) -> bool:
        return interface_kind in self._senders

    def registered_interfaces(self) -> list[str]:
        return sorted(self._senders.keys())

    async def send(self, interface_kind: str, recipient_id: str, text: str) -> dict[str, Any]:
        """Deliver text to a recipient over the named interface.

        Raises WaxNotFoundError if no interface sender is attached (honest
        failure — the caller decides what to do; the runtime never fakes a
        successful delivery).
        """
        sender = self._senders.get(interface_kind)
        if sender is None:
            raise WaxNotFoundError(f"No delivery interface attached for kind={interface_kind!r}")
        return await sender(recipient_id, text)
