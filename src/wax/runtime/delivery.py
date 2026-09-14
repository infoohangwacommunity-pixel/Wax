"""Delivery router — how the runtime sends outbound messages, interface-agnostically.

Phase W (Interface Intelligence) requires that the runtime — not WhatsApp —
understands delivery. The DeliveryRouter is the runtime's single outbound
door: interface adapters register a sender callable, and runtime mechanisms
(capabilities, background work) deliver through the router without knowing
which interface is attached.

The router knows NOTHING about WhatsApp. It knows about interface kinds,
recipients, text, and — since the constitutional audit — about policies that
attached interfaces DECLARE for themselves. A vendor delivery policy (e.g.
a freshness window on inbound contact) belongs to the interface adapter that
lives under that vendor; the runtime's job is only to enforce whatever the
attached interface declared, generically, and to refuse honestly when a
policy forbids a plain-text delivery. The runtime never invents vendor rules.

Adding Telegram/web means registering another sender (with its own policy,
or none).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from wax.core.exceptions import WaxNotFoundError
from wax.runtime.logging import get_logger

log = get_logger(__name__)

# A sender is an async callable (recipient_interface_id, text) -> provider ack dict.
SenderFn = Callable[[str, str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class DeliveryPolicy:
    """Delivery constraints DECLARED BY an interface adapter.

    The runtime enforces these generically (see message.send) but never
    supplies them itself: if no policy is attached, no vendor-specific
    restriction exists. Fields:

    - inbound_freshness_window: if set, a plain-text delivery to a recipient
      is only permitted within this window of their last inbound message.
      Outside it the adapter must explain why via freshness_note.
    - freshness_note: honest, interface-authored explanation used verbatim
      in the runtime's refusal (e.g. a vendor template requirement).
    """

    inbound_freshness_window: timedelta | None = None
    freshness_note: str = field(default="")


class DeliveryRouter:
    """Registry of interface senders + the runtime's outbound send API."""

    def __init__(self) -> None:
        self._senders: dict[str, SenderFn] = {}
        self._policies: dict[str, DeliveryPolicy] = {}

    def register(
        self,
        interface_kind: str,
        sender: SenderFn,
        policy: DeliveryPolicy | None = None,
    ) -> None:
        """Attach an interface sender, with whatever delivery policy the
        interface itself declares. Re-registering replaces silently."""
        self._senders[interface_kind] = sender
        if policy is not None:
            self._policies[interface_kind] = policy
        else:
            self._policies.pop(interface_kind, None)
        log.info("delivery.interface_registered", interface=interface_kind)

    def has(self, interface_kind: str) -> bool:
        return interface_kind in self._senders

    def registered_interfaces(self) -> list[str]:
        return sorted(self._senders.keys())

    def policy_for(self, interface_kind: str) -> DeliveryPolicy | None:
        """The delivery policy the attached interface declared, if any.

        None means the interface declared no vendor policy — the runtime
        applies none. This is the mechanism/policy boundary: vendor rules
        live with the vendor's adapter, never in runtime code.
        """
        return self._policies.get(interface_kind)

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
