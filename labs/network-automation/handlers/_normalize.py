"""
handlers/_normalize.py

Comparison-time value normalisation helpers.

Cisco IOS XE returns RESTCONF/NETCONF values in shapes that don't always
match how operators write them in changes.yaml. This module normalises
BOTH sides of every _states_match comparison to the same canonical form
so equal values compare equal regardless of source representation.

Use these helpers in every _extract_* parser AND every _desired_state
builder. Asymmetric normalisation re-introduces the bug.

Common pitfalls these helpers address:
    - VLAN id 91 (int from RESTCONF) vs "91" (str from YAML)
    - "0/0/0" vs "GigabitEthernet0/0/0" interface names
    - "  description  " vs "description" trailing whitespace
    - True / "true" / "True" boolean variants
    - 192.168.001.001 vs 192.168.1.1 zero-padded IPv4
"""

from __future__ import annotations

import ipaddress
from typing import Any


# ── Type coercion ─────────────────────────────────────────────────────────────

def normalize_int(value: Any) -> int | None:
    """
    Coerce ints, numeric strings, and bools to int.
    Returns None for None or non-numeric input — never raises.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_str(value: Any) -> str | None:
    """
    Coerce to stripped string. Returns None for None.
    Empty string after strip is preserved as "" (distinct from None).
    """
    if value is None:
        return None
    return str(value).strip()


def normalize_bool(value: Any) -> bool | None:
    """
    Accept True/False, "true"/"false" (any case), 1/0.
    Returns None for unrecognised input (not False — None is the
    'unknown' state, False is the 'explicitly false' state).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no"):
            return False
    return None


# ── Network values ────────────────────────────────────────────────────────────

def normalize_ipv4(value: Any) -> str | None:
    """
    Canonicalise and validate IPv4 address.
    Returns canonical dotted-decimal form, or None for None/invalid input.

    Note: rejects zero-padded octets (e.g. '192.168.001.001') as malformed,
    matching Python 3.9.5+ behaviour (CVE-2021-29921). If you have such
    values in YAML, fix the YAML — they're ambiguous in security contexts.
    """
    if value is None:
        return None
    try:
        return str(ipaddress.IPv4Address(str(value).strip()))
    except (ipaddress.AddressValueError, ValueError):
        return None


def normalize_mask(value: Any) -> str | None:
    """
    Canonicalise IPv4 subnet mask in dotted-decimal form.
    Accepts dotted ('255.255.255.0') or prefix-length ('24', 24).
    Always returns dotted-decimal — that's what Cisco-IOS-XE-native uses.
    Returns None for invalid input.
    """
    if value is None:
        return None
    s = str(value).strip()
    # Prefix length form: "24" or 24
    if s.isdigit():
        try:
            prefix = int(s)
            if 0 <= prefix <= 32:
                return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)
        except ValueError:
            return None
        return None
    # Dotted-decimal form: validate by canonicalising
    try:
        return str(ipaddress.IPv4Address(s))
    except (ipaddress.AddressValueError, ValueError):
        return None


# ── Cisco-specific shape coercion ─────────────────────────────────────────────

def as_list(value: Any) -> list:
    """
    Cisco RESTCONF returns YANG lists as a single dict when the list has
    exactly one entry, and as a list of dicts when it has more.
    This helper normalises both shapes to a list.

    Use at every site where you iterate a value extracted from RESTCONF JSON.

    Examples:
        as_list({"address": "1.1.1.1"})           → [{"address": "1.1.1.1"}]
        as_list([{"a": 1}, {"a": 2}])             → [{"a": 1}, {"a": 2}]
        as_list(None)                             → []
        as_list([])                               → []
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def normalize_iface_name(value: Any) -> str | None:
    """
    Normalise interface 'name' leaf to the form Cisco-IOS-XE-native uses:
    just the bare numeric part, no type prefix.
    'GigabitEthernet0/0/0' → '0/0/0'
    '0/0/0'                → '0/0/0'
    Returns None for None.
    """
    if value is None:
        return None
    s = str(value).strip()
    # Strip any leading interface-type prefix
    for prefix in ("GigabitEthernet", "TenGigabitEthernet", "FortyGigabitEthernet",
                   "Loopback", "Vlan", "Port-channel", "Tunnel"):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s
