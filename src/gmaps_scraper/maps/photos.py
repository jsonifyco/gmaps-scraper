"""Place photo gallery (``hspqX``) and per-photo author lookups (``photometa``).

The gallery RPC is answered for page XHRs without the review-style bgkey, so
pages are requested directly: 100 photos per request, continuation token at
``inner[4][2][2]`` (verified 2026-10-01: page 2 with the token shares no ids
with page 1).  Gallery items carry no author; with ``scrapeImageAuthors`` each
photo is looked up through ``/maps/photometa/v1`` like the Apify actor does.
"""

from __future__ import annotations

import re
import urllib.parse as up
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import pb
from .xssi import ResponseFormatError, dig, loads_guarded, parse_batchexecute

PARSER_VERSION = "photos-2026.10.11"  # date field is retained without assuming upload semantics
FULL_SIZE = "=w1920-h1080-k-no"
#: the trailing size directive; a panorama's carries dots and more options
#: (``=w203-h100-k-no-pi15.468751-ya334-ro-0-fo100``), so anything after the last "=" up to the end
_SIZE_SUFFIX = re.compile(r"=[a-z][^/=]*$", re.I)


def full_size(url: str | None) -> str | None:
    """Replace the thumbnail directive by the size Apify reports."""
    if not url:
        return url
    if url.startswith("//"):
        url = "https:" + url
    if "googleusercontent.com" not in url:
        return url
    return _SIZE_SUFFIX.sub("", url) + FULL_SIZE


@dataclass
class GalleryPage:
    photos: list[dict[str, Any]]
    next_token: str | None
    total: int | None
    refused: bool = False


def _upload_date(parts: Any) -> str | None:
    if not isinstance(parts, list) or len(parts) < 3 or not parts[0]:
        return None
    if any(type(p) is not int for p in parts[:3]):
        return None
    try:
        date = datetime(parts[0], parts[1], parts[2], tzinfo=timezone.utc)
        return f"{date.year:04d}-{date.month:02d}-{date.day:02d}"
    except (TypeError, ValueError):
        return None


def parse_photo(item: list) -> dict[str, Any]:
    raw_url = dig(item, 6, 0)
    raw_date = dig(item, 21, 6, 8)
    parsed_date = _upload_date(raw_date)
    precision = "day" if parsed_date else ("month" if isinstance(raw_date, list) and len(raw_date) == 2
                                           and type(raw_date[0]) is int and type(raw_date[1]) is int
                                           and 1 <= raw_date[0] <= 9999 and 1 <= raw_date[1] <= 12 else "unknown")
    return {
        "photoId": dig(item, 0),
        "imageUrl": full_size(raw_url),
        "thumbnailUrl": raw_url,
        "width": dig(item, 6, 2, 0),
        "height": dig(item, 6, 2, 1),
        "uploadedAt": None,
        "dateEvidence": {"rawValue": raw_date, "value": parsed_date, "precision": precision,
                         "meaning": "unknown", "source": "google_maps_gallery", "fieldPath": "[21][6][8]",
                         "observedAt": None, "evidenceSha256": None, "sourceUrl": None, "observations": []},
        "source": dig(item, 21, 6, 5, 2),
        "isVideo": dig(item, 1) == 3 or "video" in str(dig(item, 20) or "").lower(),
        "authorName": None,
        "authorUrl": None,
        "location": ({"lat": dig(item, 8, 0, 2), "lng": dig(item, 8, 0, 1)} if dig(item, 8, 0, 2) is not None else None),
    }


def parse_gallery(text: str) -> GalleryPage:
    envelopes = [e for e in parse_batchexecute(text) if e.rpc == "hspqX"]
    if not envelopes:
        raise ResponseFormatError("no hspqX envelope in gallery response")
    payload = envelopes[0].payload
    if payload is None:
        return GalleryPage([], None, None, refused=True)
    items = dig(payload, 0, default=[]) or []
    photos = [parse_photo(i) for i in items if isinstance(i, list) and dig(i, 0)]
    return GalleryPage(photos, dig(payload, 5) or None, dig(payload, 1))


def gallery_body(fid: str, token: str | None, page_size: int = 100) -> str:
    return pb.batchexecute_body("hspqX", pb.photos_inner(fid, token=token, page_size=page_size))


PHOTOMETA_TAIL = (
    "!4m61!1e1!1e2!1e3!1e4!1e5!1e6!1e8!1e12!1e17!2m1!1e1!4m1!1i48!5m1!1e1!5m1!1e2!6m1!1e1!6m1!1e2"
    "!9m36!1m3!1e2!2b1!3e2!1m3!1e2!2b0!3e3!1m3!1e3!2b1!3e2!1m3!1e3!2b0!3e3!1m3!1e8!2b0!3e3!1m3!1e1"
    "!2b0!3e3!1m3!1e4!2b0!3e3!1m3!1e10!2b1!3e2!1m3!1e10!2b0!3e3!11m2!3m1!4b1"
)


def photometa_url(photo_id: str, fid: str, *, hl: str, gl: str) -> str:
    pbv = (f"!1m4!1smaps_sv.tactile!11m2!2m1!1b1!2m2!1s{hl}!2s{gl}!3m5!1m2!1e10!2s{photo_id}"
           f"!2m1!5s{fid}" + PHOTOMETA_TAIL)
    return f"{pb.BASE}/maps/photometa/v1?" + up.urlencode({"authuser": "0", "hl": hl, "gl": gl, "pb": pbv},
                                                         quote_via=up.quote, safe="!:")


def parse_photometa(text: str) -> dict[str, Any]:
    data = loads_guarded(text)
    name = dig(data, 1, 0, 4, 1, 0, 0, 0)
    url = dig(data, 1, 0, 4, 1, 0, 1)
    if isinstance(url, str) and url.startswith("//"):
        url = "https:" + url
    return {"authorName": name, "authorUrl": url}


def photometa_dates(text: str, photo_id: str) -> list[dict[str, Any]]:
    """Retain both calendars without assigning upload/capture semantics globally."""
    data = loads_guarded(text)
    if dig(data, 1, 0, 1, 1) != photo_id:
        raise ResponseFormatError("photometa returned a different publication")
    result = []
    for index in (7, 8):
        raw = dig(data, 1, 0, 6, index)
        value = _upload_date(raw)
        month = (isinstance(raw, list) and len(raw) == 2 and all(type(p) is int for p in raw)
                 and 1 <= raw[0] <= 9999 and 1 <= raw[1] <= 12)
        result.append({"rawValue": raw, "value": value or (f"{raw[0]:04d}-{raw[1]:02d}" if month else None),
                       "precision": "day" if value else "month" if month else "unknown",
                       "meaning": "unknown", "source": "google_maps_photometa",
                       "fieldPath": f"[1][0][6][{index}]"})
    return result
