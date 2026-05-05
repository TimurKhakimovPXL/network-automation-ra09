"""
Small XML helpers for NETCONF payload construction.

Most handlers build IOS XE NETCONF XML with f-strings. Keep that pattern, but
escape all text-node values and validate dynamic element names before use.
"""

from __future__ import annotations

from xml.sax.saxutils import escape


ALLOWED_INTERFACE_TAGS = {
    "GigabitEthernet",
    "TenGigabitEthernet",
    "FortyGigabitEthernet",
    "Loopback",
    "Vlan",
    "Port-channel",
    "Tunnel",
}


def text(value: object) -> str:
    """Escape a value for use inside an XML text node."""
    return escape("" if value is None else str(value), {'"': "&quot;", "'": "&apos;"})


def interface_tag(value: str) -> str:
    """Validate an interface type before using it as an XML element name."""
    if value not in ALLOWED_INTERFACE_TAGS:
        allowed = ", ".join(sorted(ALLOWED_INTERFACE_TAGS))
        raise ValueError(f"Unsupported interface_type {value!r}; expected one of: {allowed}")
    return value
