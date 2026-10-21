"""Classification of Google Maps start URLs.

Supported forms (as listed by the Apify contract and seen on Maps):

* place pages ``/maps/place/<name>/@lat,lng,z/data=...!1s0x..:0x..`` (fid in data)
* ``/maps/place/?q=place_id:<id>`` and ``/maps/search/?api=1&query_place_id=<id>``
* ``?cid=<number>`` on any Maps host
* search pages ``/maps/search/<query>/@lat,lng,zoomz``
* map views ``/maps/@lat,lng,zoomz`` (viewport only)
* short links ``goo.gl/maps/...``, ``maps.app.goo.gl/...`` (resolved by opening them)
* saved lists ``/maps/placelists/list/<id>`` and ``/maps/@/data=!4m3!11m2!2s<list>``
* review links ``/maps/reviews/data=...!1s0x..:0x..`` (the place they belong to)
"""

from __future__ import annotations

import re
import urllib.parse as up

from ..input_model import StartUrl
from .ids import FID_IN_TEXT_RE, fid_from_cid, fid_from_place_id, normalize_fid

MAPS_HOSTS = ("google.", "maps.google.")
_AT = re.compile(r"/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:,(\d+(?:\.\d+)?)z)?")
_AT_METERS = re.compile(r"/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(\d+(?:\.\d+)?)m")
_LIST_ID = re.compile(r"!11m\d+!2s([A-Za-z0-9_-]{8,})")


def _is_maps_host(host: str) -> bool:
    host = host.lower()
    return host.startswith("www.google.") or host.startswith("google.") or host.startswith("maps.google.")


def _viewport(path: str) -> tuple[float | None, float | None, float | None]:
    m = _AT.search(path)
    if m:
        return float(m.group(1)), float(m.group(2)), float(m.group(3)) if m.group(3) else None
    m = _AT_METERS.search(path)
    if m:
        # "@lat,lng,1234m" (satellite) -> approximate zoom from metres of view height
        import math
        metres = float(m.group(3))
        lat = float(m.group(1))
        zoom = math.log2(156543.03392 * math.cos(math.radians(lat)) * 768 / max(metres, 1.0))
        return lat, float(m.group(2)), round(zoom, 2)
    return None, None, None


def classify_start_url(url: str) -> StartUrl:
    raw = url.strip()
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parsed = up.urlparse(raw)
    host = parsed.netloc.lower()
    path = up.unquote(parsed.path)
    query = up.parse_qs(parsed.query)

    if host in ("goo.gl", "maps.app.goo.gl") or host.endswith(".app.goo.gl"):
        return StartUrl(url=url, kind="short")
    if not _is_maps_host(host) and host != "www.google.com":
        return StartUrl(url=url, kind="unknown")
    if not (path.startswith("/maps") or "cid" in query or host.startswith("maps.google.")):
        return StartUrl(url=url, kind="unknown")

    lat, lng, zoom = _viewport(path)

    if "cid" in query and query["cid"][0].lstrip("-").isdigit():
        return StartUrl(url=url, kind="cid", fid=fid_from_cid(query["cid"][0]))

    if path.startswith("/maps/placelists/list/") or (_LIST_ID.search(path) and "/place/" not in path):
        return StartUrl(url=url, kind="list")

    qparam = (query.get("q") or query.get("query") or [""])[0]
    place_id = (query.get("query_place_id") or [None])[0]
    if not place_id and qparam.lower().startswith("place_id:"):
        place_id = qparam.split(":", 1)[1].strip()
    if place_id:
        return StartUrl(url=url, kind="place", place_id=place_id, fid=fid_from_place_id(place_id))

    if path.startswith("/maps/reviews"):
        fid = FID_IN_TEXT_RE.search(path)
        return StartUrl(url=url, kind="reviews", fid=normalize_fid(fid.group(1)) if fid else None)

    if path.startswith("/maps/place"):
        fids = FID_IN_TEXT_RE.findall(path)
        fid = normalize_fid(fids[-1]) if fids else None
        if fid and fid.startswith("0x0:"):
            fid = fid  # cid-only form is accepted by the place endpoint
        name = path.split("/maps/place/", 1)[1].split("/")[0] if "/maps/place/" in path else ""
        name = name.replace("+", " ").strip()
        return StartUrl(url=url, kind="place", fid=fid, query=name or None, lat=lat, lng=lng, zoom=zoom)

    if path.startswith("/maps/search"):
        rest = path.split("/maps/search/", 1)[1] if "/maps/search/" in path else ""
        term = rest.split("/@")[0].split("/")[0].replace("+", " ").strip() or qparam
        if query.get("api") and qparam:
            term = qparam
        return StartUrl(url=url, kind="search", query=term or None, lat=lat, lng=lng, zoom=zoom)

    if lat is not None:
        return StartUrl(url=url, kind="map", lat=lat, lng=lng, zoom=zoom)
    if qparam:
        return StartUrl(url=url, kind="search", query=qparam)
    return StartUrl(url=url, kind="unknown")
