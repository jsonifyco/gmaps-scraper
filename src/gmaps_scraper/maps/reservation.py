"""Google's table reservation page (``reserveTableUrl``: ``/maps/reserve/v/dine/c/<id>``) -> providers.

The page carries its data in ``AF_initDataCallback({key: 'ds:0', …})``; ``ds:0[8][1]`` lists one
record per bookable merchant, whose ``[0]`` was located on The Coal Hole (DesignMyNight), Monterey
Bay Fish Grotto, Alla Famiglia and Café Momentum (OpenTable), 2026-10-09
(tested against Google reservation endpoints):

    [0] merchant id   [1] place name   [5] address   [6] phone   [7] website   [8] Maps CID URL
    [9] provider block: [2] terms URL, [3] privacy URL, [11] provider id,
        [24] [provider name, [logo], ?, [logo]]
    [10] time zone    [11] [?, ?, [?, ?, booking terms URL]]   [17] reservation id   [25] fid
    [28] kgmid        [32] country code

The address, phone and website of the record are the restaurant's (they equal the place card),
not the provider's: they are never reported as provider contacts.  The page names no email,
phone or address of the provider itself, so those stay unknown (``None``).

Identity: with an expected ``fid`` every record must prove it belongs to that place -- its ``[25]``
equals the fid, or, when ``[25]`` is missing, the CID of its Maps URL ``[8]`` equals the fid's CID.
A record that names another place, or proves nothing, is a :class:`ReserveIdentityError`; any other
shape is :class:`ReservePageUnreadable`.
"""

from __future__ import annotations

import json
import re
import urllib.parse as up
from dataclasses import dataclass
from typing import Any

from .ids import cid_from_fid
from .xssi import dig

PARSER_VERSION = "reservation-2026.10.09.2"
_DS0 = re.compile(r"AF_initDataCallback\(\{key: 'ds:0'.*?data:(.*?), sideChannel", re.S)


class ReservePageUnreadable(Exception):
    """The answer is not a reservation page of the known shape."""


class ReserveIdentityError(ReservePageUnreadable):
    """The page belongs to another place."""


@dataclass
class ReserveProvider:
    name: str
    provider_id: str | None
    merchant_id: str | None
    reservation_id: str | None
    terms_url: str | None
    privacy_url: str | None
    booking_terms_url: str | None
    logo: str | None


def parse_reserve_page(html: str, *, fid: str | None = None) -> list[ReserveProvider]:
    m = _DS0.search(html)
    if not m:
        raise ReservePageUnreadable("no ds:0 data block (not a reservation page)")
    try:
        data = json.loads(m.group(1))
    except ValueError as exc:
        raise ReservePageUnreadable(f"ds:0 is not JSON: {exc}") from exc
    records = dig(data, 8, 1)
    if not isinstance(records, list) or not records:
        raise ReservePageUnreadable("ds:0[8][1] holds no merchant record")
    out: list[ReserveProvider] = []
    for rec in records:
        r0 = dig(rec, 0)
        if not isinstance(r0, list) or len(r0) < 26:
            raise ReservePageUnreadable(f"merchant record of {len(r0) if isinstance(r0, list) else type(r0).__name__} "
                                        "elements (expected at least 26)")
        if fid:
            _check_identity(r0, fid)
        block = r0[9]
        name = dig(block, 24, 0)
        if not isinstance(block, list) or not isinstance(name, str) or not name:
            raise ReservePageUnreadable("provider block without a provider name at [9][24][0]")
        out.append(ReserveProvider(
            name=name, provider_id=dig(block, 11), merchant_id=r0[0] if isinstance(r0[0], str) else None,
            reservation_id=r0[17] if len(r0) > 17 and isinstance(r0[17], str) else None,
            terms_url=dig(block, 2), privacy_url=dig(block, 3), booking_terms_url=dig(r0, 11, 2, 2),
            logo=dig(block, 24, 3, 0) or dig(block, 24, 1, 0)))
    return out


def _record_cid(r0: list) -> str | None:
    url = r0[8] if isinstance(r0[8], str) else None
    if not url:
        return None
    cid = (up.parse_qs(up.urlparse(url).query).get("cid") or [None])[0]
    return cid if cid and cid.isdigit() else None


def _check_identity(r0: list, fid: str) -> None:
    rec_fid = r0[25]
    if isinstance(rec_fid, str) and rec_fid:
        if rec_fid.lower() != fid.lower():
            raise ReserveIdentityError(f"the reservation page belongs to {rec_fid}, not {fid}")
        return
    if rec_fid is not None and rec_fid != "":
        raise ReserveIdentityError(f"the reservation record's fid [25] is a {type(rec_fid).__name__}, not a string")
    want, got = cid_from_fid(fid), _record_cid(r0)
    if got is None:
        raise ReserveIdentityError(f"the reservation record names no place (no fid [25], no CID URL [8]); expected {fid}")
    if got != want:
        raise ReserveIdentityError(f"the reservation page belongs to CID {got}, not {fid} (CID {want})")


def provider_entry(p: ReserveProvider, *, reserve_url: str | None) -> dict[str, Any]:
    """An Apify ``tableReservationProviders`` entry with the native extras of its source."""
    return {"name": p.name, "email": None, "phone": None, "address": None, "reserveTableUrl": reserve_url,
            "providerId": p.provider_id, "termsUrl": p.terms_url, "privacyUrl": p.privacy_url,
            "bookingTermsUrl": p.booking_terms_url,
            "sources": {"name": "google_reserve_page", "contacts": "not_in_source"}}
