"""XML pretty-printing utilities for draw.io output.

Adapted from the DrawIoGen reference implementation.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET


def indent(elem: ET.Element, level: int = 0) -> None:
    """Indent an ElementTree element and its children in-place."""
    pad = "  "
    prefix = "\n" + level * pad
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = prefix + pad
        for child in elem:
            indent(child, level + 1)
        if not elem[-1].tail or not elem[-1].tail.strip():
            elem[-1].tail = prefix
    if level and (not elem.tail or not elem.tail.strip()):
        elem.tail = prefix


def to_string(root: ET.Element) -> str:
    """Pretty-print an ElementTree element to a string."""
    indent(root)
    return ET.tostring(root, encoding="unicode") + "\n"
