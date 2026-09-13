"""wax.runtime.bridge — canonical bridge between interfaces and runtime.

This is the universal entrypoint that ANY interface adapter (WhatsApp,
web, Telegram, future) calls to submit a user message to the runtime.

The bridge:
1. Deduplicates incoming messages (idempotency by interface_message_id)
2. Resolves interface-specific identity → WAX Principal (creating a
   principal + credential on first contact)
3. Builds a canonical RuntimeRequest (interface-agnostic)
4. Hands off to the runtime composition (objective + execution + intelligence)
5. Returns a canonical RuntimeResponse that the interface adapter can
   render in its platform-specific shape

INVARIANTS:
- One user message → one runtime execution (deduplication enforced)
- The bridge is interface-agnostic; it accepts RuntimeRequest, not
  WhatsApp-specific shapes.
- Interface adapters translate platform messages INTO RuntimeRequest,
  call this bridge, and translate RuntimeResponse OUT to platform shape.
"""

from wax.runtime.bridge.contracts import (
    InterfaceKind,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeResponseStatus,
)
from wax.runtime.bridge.service import RuntimeBridge
from wax.runtime.bridge.store import ProcessedMessageRecord

__all__ = [
    "InterfaceKind",
    "RuntimeRequest",
    "RuntimeResponse",
    "RuntimeResponseStatus",
    "RuntimeBridge",
    "ProcessedMessageRecord",
]
