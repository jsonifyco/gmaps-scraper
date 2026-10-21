# Discovery & Map Coverage

Google Maps limits search results to approximately 120–165 places per viewport. To scrape larger cities, territories, or dense urban districts without missing pins, `gmaps-scraper` implements intelligent multi-pass spatial partitioning and dynamic grid subdivision.

---

## 1. Defining Locations

You can define the search territory in three ways:

### Free-Text Location (`locationQuery`)
Natural language location string (e.g., `"Munich, Germany"` or `"Shoreditch, London"`). The scraper resolves the boundary polygon via OpenStreetMap Nominatim:
- Caches polygons locally in `~/.cache/gmaps-scraper-bro/nominatim`.
- Enforces OSM's usage policy (max 1 request/sec).
- If Nominatim returns multiple ambiguous matches, the scraper exits with candidate IDs so you can pin the exact boundary using `osmId` (e.g., `"R62422"`).

### Structured Geolocation
Separate fields: `countryCode`, `state`, `county`, `city`, and `postalCode`.
*Tip:* For best precision, pair `countryCode` with `postalCode`, or `countryCode` with `city`.

### Custom GeoJSON (`customGeolocation`)
Provide an exact custom geometry without external geocoding (see `examples/top-n-search.json`):
- **Radial circle:** `{"type": "Point", "coordinates": [-73.9855, 40.758], "radiusKm": 1.5}`
- **Arbitrary boundary:** Standard GeoJSON `Polygon` or `MultiPolygon`.

---

## 2. Coverage Levels

Configured via `bro.coverage` in `input.json` (see `examples/deep-coverage.json`):

```json
{
  "bro": {
    "coverage": "deep"
  }
}
```

| Level | Strategy | Passes Included | Recommended For |
| --- | --- | --- | --- |
| `standard` | `legacy-v1` | Overview pass + Base grid | Quick scans, sparse regions, or when capped by `maxCrawledPlaces`. |
| `deep` | `adaptive-v4` | Overview + Base grid + Dynamic cell split + Offset control pass | Thorough scraping of cities, dense downtowns, or exhaustive lead lists. |
| `extended` | `adaptive-v4` | All of `deep` + Related category discovery pass | Broad industry harvests where businesses may be listed under allied categories. |

### The Overview Pass
Before entering the grid, the scraper inspects a wide viewport covering the entire territory. This ensures the first $N$ places in a search follow Google's natural organic ranking for the whole area rather than the arbitrary order of individual grid cells.

### Adaptive Grid Subdivision (`adaptive-v4`)
Google caps viewport search results at ~110–170 items. When a cell search hits either `saturationThreshold` (default 40 in-cell places) or Google's viewport limit:
1. The cell automatically splits into 4 sub-cells of half the width and height.
2. Subdivision repeats recursively until places within the cell are no longer saturated or the cell size reaches `minCellKm` (default `0.4` km, configurable down to `0.05` km).
3. If an unresolved cell reaches the minimum size, it is marked in `report.json` as `saturated_unsplittable` so you have complete visibility over potential density bottlenecks.

### The Offset Control Pass
Pins sitting close to the borders of grid cells can be clipped by Google's clustering algorithms. In `deep` and `extended` modes, the scraper calculates an offset pass: every leaf cell is shifted diagonally by 50% of its width. This consistently recovers an additional ~10% of unique places that base grids miss.

### Extended Category Expansion
In `extended` mode, after finishing the main search terms, the scraper analyzes the primary categories of all accepted places. It automatically schedules searches for the top 5 most common related categories (e.g., discovering `"espresso bar"` and `"patisserie"` when searching for `"bakery"`).

---

## 3. Direct Targets & URL Resolution

Instead of searching an area, you can feed direct targets:
- **`startUrls`**: Paste Google Maps search URLs (`.../maps/search/restaurants/@40.75,-73.98,15z`), place URLs (`.../maps/place/...`), CID URLs (`.../?cid=...`), short links (`goo.gl/maps/...`), or saved list URLs.
- **`placeIds`**: Pass arrays of Google Place IDs (`ChIJ...`). Standard IDs are decoded to internal identifiers offline without network latency; unresolved IDs are navigated once in the browser.

---

## 4. "All Places" Map Label Extraction

To extract every business pin in a neighborhood without any search term:
```json
{
  "locationQuery": "Little Italy, New York, NY",
  "allPlacesNoSearchAction": "all_places_no_search_mouse",
  "bro": {
    "allPlacesZoom": 17.0
  }
}
```
The scraper tiles the area at zoom 17, inspects rendered map pins directly using bro's visual extraction layer, and verifies each detected place against Google Maps records.

---

## 5. Interpreting Coverage in `report.json`

The coverage section of `report.json` provides an audit trail for every search term:

```json
"coverage": {
  "restaurant": {
    "cells": {
      "processed": 42,
      "saturated_split": 8,
      "unresolved_min_size": 2
    },
    "uniquePlacesSeen": 580,
    "accepted": 520,
    "rejected": {
      "outside_area": 45,
      "minimum_stars": 15
    },
    "newPlacesByPass": {
      "overview": 20,
      "main": 450,
      "offset": 50
    }
  }
}
```
- **`newPlacesByPass`**: Shows exactly how many places were contributed by the overview pass, main grid, and offset control pass.
- **`rejected`**: Detailed breakdown of why pins were filtered out (e.g., fell outside boundary polygon, closed, failed star filter).
