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

# Interface types that are config, not hardware. A 404 on the
# initial RESTCONF read means "not yet created in running-config";
# the first edit-config write will create the interface. Handlers
# treat 404-on-virtual as "current state is absent" rather than
# an error. Anything not in this set is treated as physical: a
# 404 means the interface doesn't exist as hardware and is a real
# error (typo, wrong platform, etc.).
VIRTUAL_INTERFACE_TAGS = {
    "Loopback",
    "Tunnel",
    "Vlan",
    "Port-channel",
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
