"""Search-area geometry.

Areas are kept as shapely geometries in lon/lat (EPSG:4326) together with the
GeoJSON they came from.  Circles (``Point`` + ``radiusKm``) and line buffers
are built geodesically in a local azimuthal-equidistant projection, so a
5 km radius is 5 km at any latitude.

Containment is inclusive: a place exactly on the boundary belongs to the
area.  Areas are validated for coordinate order: GeoJSON is ``[lng, lat]``
and an input whose second coordinate exceeds +-90 is rejected with a hint.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from pyproj import CRS, Geod, Transformer
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import transform, unary_union
from shapely.validation import make_valid

GEOD = Geod(ellps="WGS84")
DEFAULT_RADIUS_KM = 5.0       # Apify: a Point is a circle of 5 km by default
LINE_BUFFER_KM = 0.5          # LineString areas: documented corridor half-width


class GeometryError(ValueError):
    pass


@dataclass
class Area:
    geometry: Any                          # shapely Polygon / MultiPolygon in lon/lat
    source: str                            # custom | nominatim | viewport | circle
    label: str
    geojson: dict[str, Any]
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def area_km2(self) -> float:
        return geodesic_area_km2(self.geometry)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.geometry.bounds  # minx (lng), miny (lat), maxx, maxy

    def contains(self, lat: float | None, lng: float | None) -> bool | None:
        if lat is None or lng is None:
            return None
        return bool(self.geometry.covers(Point(lng, lat)))

    def to_json(self) -> dict[str, Any]:
        return {"source": self.source, "label": self.label, "geometry": mapping(self.geometry),
                "provenance": self.provenance, "areaKm2": round(self.area_km2, 3)}


def geodesic_area_km2(geom: Any) -> float:
    if geom.is_empty:
        return 0.0
    area, _ = GEOD.geometry_area_perimeter(geom)
    return abs(area) / 1e6


def _local_projection(lat: float, lng: float) -> tuple[Transformer, Transformer]:
    aeqd = CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lng} +datum=WGS84 +units=m +no_defs")
    fwd = Transformer.from_crs("EPSG:4326", aeqd, always_xy=True)
    inv = Transformer.from_crs(aeqd, "EPSG:4326", always_xy=True)
    return fwd, inv


def circle(lat: float, lng: float, radius_km: float) -> Polygon:
    if radius_km <= 0:
        raise GeometryError("radiusKm must be positive")
    fwd, inv = _local_projection(lat, lng)
    disk = Point(0, 0).buffer(radius_km * 1000.0, quad_segs=32)
    return transform(inv.transform, disk)


def buffer_km(geom: Any, km: float) -> Any:
    c = geom.centroid
    fwd, inv = _local_projection(c.y, c.x)
    return transform(inv.transform, transform(fwd.transform, geom).buffer(km * 1000.0, quad_segs=16))


def _check_coords(coords: Any, path: str = "coordinates") -> None:
    if isinstance(coords, (list, tuple)) and coords and isinstance(coords[0], (int, float, str)):
        if len(coords) < 2:
            raise GeometryError(f"{path}: a position needs [longitude, latitude]")
        try:
            lng, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError) as exc:
            raise GeometryError(f"{path}: not a number: {coords[:2]}") from exc
        if not -180 <= lng <= 180:
            raise GeometryError(f"{path}: longitude {lng} out of range")
        if not -90 <= lat <= 90:
            raise GeometryError(f"{path}: latitude {lat} out of range -- GeoJSON order is [longitude, latitude]")
        return
    if isinstance(coords, (list, tuple)):
        for i, c in enumerate(coords):
            _check_coords(c, f"{path}[{i}]")
        return
    raise GeometryError(f"{path}: invalid coordinates")


def _numeric(coords: Any) -> Any:
    if isinstance(coords, (list, tuple)) and coords and isinstance(coords[0], (int, float, str)):
        return [float(coords[0]), float(coords[1])]
    return [_numeric(c) for c in coords]


def _to_area_geometry(obj: dict[str, Any], default_radius_km: float) -> Any:
    kind = obj.get("type")
    if kind == "Feature":
        geom = obj.get("geometry") or {}
        props = obj.get("properties") or {}
        radius = props.get("radiusKm", geom.get("radiusKm", obj.get("radiusKm", default_radius_km)))
        return _to_area_geometry({**geom, "radiusKm": radius} if geom.get("type") in ("Point", "MultiPoint") else geom,
                                 default_radius_km)
    if kind == "FeatureCollection":
        parts = [_to_area_geometry(f, default_radius_km) for f in obj.get("features") or []]
        if not parts:
            raise GeometryError("FeatureCollection has no features")
        return unary_union(parts)
    if kind == "GeometryCollection":
        parts = [_to_area_geometry(g, default_radius_km) for g in obj.get("geometries") or []]
        if not parts:
            raise GeometryError("GeometryCollection is empty")
        return unary_union(parts)
    if kind not in ("Polygon", "MultiPolygon", "LineString", "MultiLineString", "Point", "MultiPoint"):
        raise GeometryError(f"unsupported GeoJSON type {kind!r}")
    coords = obj.get("coordinates")
    if coords is None:
        raise GeometryError(f"{kind} has no coordinates")
    _check_coords(coords)
    coords = _numeric(coords)
    radius = float(obj.get("radiusKm", default_radius_km) or default_radius_km)
    if kind == "Point":
        return circle(coords[1], coords[0], radius)
    if kind == "MultiPoint":
        return unary_union([circle(c[1], c[0], radius) for c in coords])
    if kind == "LineString":
        return buffer_km(LineString(coords), LINE_BUFFER_KM)
    if kind == "MultiLineString":
        return buffer_km(MultiLineString(coords), LINE_BUFFER_KM)
    geom = shape({"type": kind, "coordinates": coords})
    if kind == "Polygon":
        for ring in coords:
            if len(ring) < 4 or ring[0] != ring[-1]:
                raise GeometryError("Polygon rings must be closed: the first and last positions must be equal")
    else:
        for poly in coords:
            for ring in poly:
                if len(ring) < 4 or ring[0] != ring[-1]:
                    raise GeometryError("MultiPolygon rings must be closed: the first and last positions must be equal")
    if not geom.is_valid:
        geom = make_valid(geom)
    return geom


def _polygonal(geom: Any) -> Any:
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if isinstance(geom, GeometryCollection):
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))]
        if polys:
            return unary_union(polys)
    raise GeometryError("the area has no surface (only points or lines after validation)")


def area_from_geojson(obj: dict[str, Any], *, label: str = "customGeolocation", source: str = "custom",
                      default_radius_km: float = DEFAULT_RADIUS_KM) -> Area:
    if not isinstance(obj, dict) or "type" not in obj:
        raise GeometryError("customGeolocation must be a GeoJSON object with a 'type'")
    geom = _polygonal(_to_area_geometry(obj, default_radius_km))
    if geom.is_empty:
        raise GeometryError("the area is empty")
    return Area(geometry=geom, source=source, label=label, geojson=json.loads(json.dumps(obj)))


def viewport_area(lat: float, lng: float, height_m: float, width_m: float, *, label: str) -> Area:
    """Rectangle around a map view (used for search URLs with ``@lat,lng,zoom``)."""
    dlat = (height_m / 2) / 111_320.0
    dlng = (width_m / 2) / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    poly = Polygon([(lng - dlng, lat - dlat), (lng + dlng, lat - dlat), (lng + dlng, lat + dlat),
                    (lng - dlng, lat + dlat), (lng - dlng, lat - dlat)])
    return Area(geometry=poly, source="viewport", label=label, geojson=mapping(poly))
