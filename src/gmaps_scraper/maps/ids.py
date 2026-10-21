"""Google Maps place identifiers and their conversions.

* ``fid`` (feature id): ``0x<hex>:0x<hex>``; the second half is the CID.
  The place endpoint also accepts ``0x0:0x<cid>`` and answers with the full
  fid, so a CID alone is enough to fetch a place.
* ``cid``: the decimal form of the second half of the fid.
* ``placeId``: Google's opaque id.  The common ``ChIJ``/``GhIJ`` form is the
  URL-safe base64 of a small protobuf that carries both fid halves as
  little-endian fixed64 fields, so it converts to a fid without a request.
  Other forms (longer ids, address ids) are kept as opaque strings and are
  resolved by opening the place page.  Place ids are stored as plain
  strings without any length rule: Google documents that they vary in
  length and may change over time.
"""

from __future__ import annotations

import base64
import re
import struct

FID_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}:0x[0-9a-fA-F]{1,16}$")
FID_IN_TEXT_RE = re.compile(r"(0x[0-9a-fA-F]{1,16}:0x[0-9a-fA-F]{1,16})")


def is_fid(value: str | None) -> bool:
    return bool(value) and bool(FID_RE.match(value))


def normalize_fid(fid: str) -> str:
    """Lower-case hex with no leading zeros, the form Google returns."""
    left, right = fid.split(":")
    return f"0x{int(left, 16):x}:0x{int(right, 16):x}"


def cid_from_fid(fid: str | None) -> str | None:
    if not is_fid(fid):
        return None
    return str(int(fid.split(":")[1], 16))


def fid_from_cid(cid: str | int) -> str:
    """The lookup form of a CID: ``0x0:0x<hex>``."""
    value = int(cid)
    if value < 0:
        value &= (1 << 64) - 1
    return f"0x0:0x{value:x}"


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def fid_from_place_id(place_id: str | None) -> str | None:
    """Decode a ``ChIJ``/``GhIJ`` style place id into a fid, else ``None``.

    The decoded bytes are ``<tag> 0x12 0x09 <8 bytes> 0x11 <8 bytes>``:
    a length-delimited message (field 1 or 3) holding two fixed64 fields.
    """
    if not place_id or len(place_id) < 20:
        return None
    try:
        raw = _b64decode(place_id)
    except (ValueError, base64.binascii.Error):
        return None
    if len(raw) != 20 or raw[0] not in (0x0A, 0x1A) or raw[1] != 0x12 or raw[2] != 0x09 or raw[11] != 0x11:
        return None
    left = struct.unpack("<Q", raw[3:11])[0]
    right = struct.unpack("<Q", raw[12:20])[0]
    return f"0x{left:x}:0x{right:x}"


def place_id_from_fid(fid: str | None) -> str | None:
    """The ``ChIJ`` id that encodes ``fid`` (used only to cross-check)."""
    if not is_fid(fid):
        return None
    left, right = (int(x, 16) for x in fid.split(":"))
    if left == 0:
        return None
    raw = bytes([0x0A, 0x12, 0x09]) + struct.pack("<Q", left) + bytes([0x11]) + struct.pack("<Q", right)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def find_fid(text: str | None) -> str | None:
    if not text:
        return None
    match = FID_IN_TEXT_RE.search(text)
    return normalize_fid(match.group(1)) if match else None
