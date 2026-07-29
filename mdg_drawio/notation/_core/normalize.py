"""Style normalization and fingerprinting.

A fingerprint binds a registry entry to its palette cell. It is a hash of the
NORMALIZED style string, so it is stable under token reordering but sensitive
to any token change. Colors and all other tokens are kept: some palettes
distinguish shapes by color alone (e.g. C4 Person vs Person_Ext).

The same normalization must be used by the registry migration, the styles
sidecar builder, and the reverse (drawio -> DSL) index — keep it here and
nowhere else.
"""
from __future__ import annotations

import hashlib

FINGERPRINT_PREFIX = "sha1:"
FINGERPRINT_HEX_LEN = 12


def normalize_style(style: str) -> str:
    """Canonical form of a draw.io style string: sorted, deduplicated tokens.

    draw.io styles are ';'-separated tokens, either bare ("rounded") or
    key=value ("fillColor=#083F75"). Token order is not significant to
    rendering, so sorting gives a stable identity.
    """
    tokens = sorted({t.strip() for t in style.split(";") if t.strip()})
    return ";".join(tokens)


def style_fingerprint(style: str) -> str:
    """Fingerprint of a raw style string, e.g. 'sha1:3fa4b2c19e07'."""
    digest = hashlib.sha1(normalize_style(style).encode("utf-8")).hexdigest()
    return FINGERPRINT_PREFIX + digest[:FINGERPRINT_HEX_LEN]
