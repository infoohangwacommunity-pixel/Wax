"""Phone number normalization — critical for principal reuse.

WhatsApp sometimes sends phone numbers as:
- 2349138153604 (no +)
- +2349138153604 (with +)
- 234 913 815 3604 (with spaces)

If the format differs between storage and lookup, the database finds
no match and creates a NEW principal every time. This is why memory
doesn't work — the AI never sees the same user twice.

This module normalizes phone numbers to a single canonical format:
+<digits_only>

Example: "+234 913-815-3604" → "+2349138153604"
         "2349138153604"     → "+2349138153604"
"""

from __future__ import annotations

import re


def normalize_phone(phone: str) -> str:
    """Normalize a phone number to canonical format: +<digits>.

    Strips all spaces, dashes, parentheses. If the result starts with +,
    keeps it. If not, adds +. This ensures the same person always
    resolves to the same principal.
    """
    if not phone:
        return ""
    # Extract only digits and leading +
    digits = re.sub(r"[^\d]", "", phone)
    if not digits:
        return ""
    return "+" + digits
