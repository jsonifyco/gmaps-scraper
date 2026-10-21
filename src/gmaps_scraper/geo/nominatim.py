"""Territory lookup through Nominatim (OpenStreetMap).

Used once per run, only to turn the user's own location input into an area
polygon -- never to look up businesses and never per grid cell.  The public
service's usage policy (https://operations.osmfoundation.org/policies/nominatim/)
is followed: at most one request per second (enforced across processes by a
lock file), an identifying User-Agent, results cached on disk, and the
endpoint is replaceable (``bro.nominatimUrl``).  Results carry the ODbL
attribution in their provenance.

An ambiguous answer is an error listing the candidates; the user picks one
with ``bro.osmId`` (e.g. ``"R62422"``).  A city is never silently widened to
its country: if the structured lookup finds nothing, nothing is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .. import __version__
from .geometry import Area, GeometryError, area_from_geojson

ATTRIBUTION = "Data © OpenStreetMap contributors, ODbL 1.0. https://osm.org/copyright"
USER_AGENT = f"gmaps-scraper-bro/{__version__} (territory lookup; python-requests)"
AREA_TYPES = {"Polygon", "MultiPolygon"}


class LocationError(ValueError):
    def __init__(self, message: str, candidates: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


@dataclass
class Candidate:
    osm_id: str
    display_name: str
    category: str
    type: str
    importance: float
    place_rank: int | None
    geojson: dict[str, Any] | None
    bbox: list[float] | None

    def summary(self) -> dict[str, Any]:
        return {"osmId": self.osm_id, "name": self.display_name, "class": self.category, "type": self.type,
                "importance": round(self.importance, 4), "hasArea": bool(self.geojson and self.geojson.get("type") in AREA_TYPES)}


def cache_dir() -> Path:
    base = os.environ.get("GMAPS_SCRAPER_CACHE") or os.path.join(Path.home(), ".cache", "gmaps-scraper-bro")
    path = Path(base) / "nominatim"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Nominatim:
    MIN_INTERVAL = 1.05

    def __init__(self, base_url: str = "https://nominatim.openstreetmap.org", *, contact: str | None = None,
                 session: requests.Session | None = None, cache: Path | None = None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        ua = USER_AGENT + (f"; contact: {contact}" if contact else "")
        self.session.headers.update({"User-Agent": ua, "Accept-Language": "en"})
        self.cache = cache or cache_dir()
        self.requests_made = 0

    # ----------------------------------------------------------------- http
    def _throttle(self) -> None:
        stamp = self.cache / ".last_request"
        lock = self.cache / ".lock"
        deadline = time.time() + 30
        while True:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                if time.time() - lock.stat().st_mtime > 10 or time.time() > deadline:
                    try:
                        lock.unlink()
                    except FileNotFoundError:
                        pass
                time.sleep(0.2)
        try:
            last = float(stamp.read_text()) if stamp.exists() else 0.0
            wait = self.MIN_INTERVAL - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            stamp.write_text(str(time.time()))
        finally:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        key = hashlib.sha256(json.dumps([self.base_url, path, sorted(params.items())]).encode()).hexdigest()
        cached = self.cache / f"{key}.json"
        if cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))
        self._throttle()
        resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=60)
        self.requests_made += 1
        if resp.status_code != 200:
            raise LocationError(f"Nominatim answered HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        cached.write_text(json.dumps(data), encoding="utf-8")
        return data

    # ---------------------------------------------------------------- lookup
    def search(self, *, query: str | None = None, structured: dict[str, str] | None = None,
               limit: int = 5) -> list[Candidate]:
        params: dict[str, Any] = {"format": "jsonv2", "polygon_geojson": 1, "limit": limit, "addressdetails": 0,
                                  "polygon_threshold": 0.0005}
        if query:
            params["q"] = query
        else:
            params.update({k: v for k, v in (structured or {}).items() if v})
        rows = self._get("/search", params)
        return [self._candidate(r) for r in rows or []]

    def lookup_osm(self, osm_id: str) -> Candidate:
        osm = osm_id.strip().upper()
        if osm[:1] not in "NWR" or not osm[1:].isdigit():
            raise LocationError(f"bro.osmId must look like R62422 / W123 / N1, got {osm_id!r}")
        rows = self._get("/lookup", {"osm_ids": osm, "format": "jsonv2", "polygon_geojson": 1,
                                     "polygon_threshold": 0.0005})
        if not rows:
            raise LocationError(f"Nominatim has no object {osm}")
        return self._candidate(rows[0])

    @staticmethod
    def _candidate(row: dict[str, Any]) -> Candidate:
        prefix = {"relation": "R", "way": "W", "node": "N"}.get(row.get("osm_type"), "?")
        bbox = [float(x) for x in row.get("boundingbox") or []] or None
        return Candidate(osm_id=f"{prefix}{row.get('osm_id')}", display_name=row.get("display_name", ""),
                         category=row.get("category") or row.get("class") or "", type=row.get("type", ""),
                         importance=float(row.get("importance") or 0.0), place_rank=row.get("place_rank"),
                         geojson=row.get("geojson"), bbox=bbox)


def choose(candidates: list[Candidate], *, label: str) -> Candidate:
    """The single unambiguous area among ``candidates`` or :class:`LocationError`."""
    areas = [c for c in candidates if c.geojson and c.geojson.get("type") in AREA_TYPES]
    if not areas:
        if candidates:
            raise LocationError(f"'{label}' resolves only to points, not to an area; use customGeolocation "
                                f"or bro.osmId", [c.summary() for c in candidates])
        raise LocationError(f"'{label}' was not found by Nominatim")
    if len(areas) == 1:
        return areas[0]
    first, second = areas[0], areas[1]
    if first.osm_id == second.osm_id:
        return first
    clear_lead = first.importance - second.importance >= 0.15 or first.importance >= 1.5 * max(second.importance, 1e-6)
    if clear_lead:
        return first
    raise LocationError(f"'{label}' is ambiguous; pick one with bro.osmId", [c.summary() for c in areas])


def resolve_area(nominatim: Nominatim, *, query: str | None = None, structured: dict[str, str] | None = None,
                 osm_id: str | None = None, label: str) -> Area:
    if osm_id:
        cand = nominatim.lookup_osm(osm_id)
    else:
        cand = choose(nominatim.search(query=query, structured=structured), label=label)
    if not cand.geojson or cand.geojson.get("type") not in AREA_TYPES:
        raise LocationError(f"{cand.osm_id} ({cand.display_name}) has no area polygon")
    try:
        area = area_from_geojson(cand.geojson, label=label, source="nominatim")
    except GeometryError as exc:
        raise LocationError(f"unusable polygon for {cand.osm_id}: {exc}") from exc
    area.provenance = {"service": nominatim.base_url, "osmId": cand.osm_id, "displayName": cand.display_name,
                       "class": cand.category, "type": cand.type, "importance": cand.importance,
                       "query": query, "structured": structured, "attribution": ATTRIBUTION}
    return area
