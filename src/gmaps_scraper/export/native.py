"""``native`` export: places, reviews, images, contacts and discoveries as separate files,
with every section's status and every value's provenance; plus flat CSV views."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterator

from ..input_model import RunInput
from ..store import Store, loads
from .apify import place_view

FLAT_PLACE_COLUMNS = [
    "fid", "placeId", "cid", "kgmid", "title", "subTitle", "categoryName", "categories", "address", "neighborhood",
    "street", "city", "postalCode", "state", "countryCode", "lat", "lng", "plusCode", "phone", "phoneUnformatted",
    "website", "totalScore", "reviewsCount", "imagesCount", "price", "permanentlyClosed", "temporarilyClosed",
    "wasOpenAtScrapeTime", "locatedIn", "floor", "menu", "servicesLink", "reserveTableUrl", "url",
    "searchKeys", "accepted", "filterReasons", "reviewsStatus", "reviewsCollected", "imagesStatus", "contactsStatus",
    "emails", "phones", "socials",
]


def _sections(store: Store, fid: str) -> dict[str, Any]:
    out = {}
    for r in store.db.execute("SELECT * FROM sections WHERE fid = ?", (fid,)):
        out[r["section"]] = {"status": r["status"], "reason": r["reason"], "count": r["count"],
                             "expected": r["expected"], "detail": loads(r["detail"])}
    return out


def native_places(store: Store, *, accepted_only: bool = False) -> Iterator[dict[str, Any]]:
    fids = [r[0] for r in store.db.execute(
        "SELECT fid FROM results GROUP BY fid " + ("HAVING MAX(accepted) = 1 " if accepted_only else "") +
        "ORDER BY MIN(created_at)")]
    for fid in fids:
        data = place_view(store, fid)
        results = [dict(fid=r["fid"], searchKey=r["search_key"], searchTerm=r["search_term"], rank=r["rank"],
                        accepted=None if r["accepted"] is None else bool(r["accepted"]), filterReason=r["filter_reason"],
                        insideArea=None if r["inside_area"] is None else bool(r["inside_area"]),
                        inputStartUrl=r["input_start_url"], inputPlaceId=r["input_place_id"], parentFid=r["parent_fid"])
                   for r in store.db.execute("SELECT * FROM results WHERE fid = ? ORDER BY created_at", (fid,))]
        discoveries = store.db.execute("SELECT COUNT(*) FROM discoveries WHERE fid = ?", (fid,)).fetchone()[0]
        contacts = [dict(kind=r["kind"], value=r["value"], domain=r["domain"], sourceUrl=r["source_url"],
                         ownership=r["ownership"], context=loads(r["context"]))
                    for r in store.db.execute("SELECT * FROM contacts WHERE fid = ?", (fid,))]
        row = store.place(fid)
        extras = {e["kind"]: {"data": loads(e["data"]), "sourceUrl": e["source_url"], "observedAt": e["observed_at"],
                              "parserVersion": e["parser_version"], "evidenceSha256": e["evidence_sha"]}
                  for e in store.db.execute("SELECT * FROM extras WHERE fid = ?", (fid,))}
        yield {"fid": fid, **data, "results": results, "discoveries": discoveries, "sections": _sections(store, fid),
               "contacts": contacts, "extras": extras, "parseErrors": loads(row["parse_errors"]) if row else None,
               "parserVersion": row["parser_version"] if row else None}


def native_reviews(store: Store) -> Iterator[dict[str, Any]]:
    from ..pipeline.review_dates import iso_us, merged_review
    dates = {(d["fid"], d["review_id"]): d for d in store.db.execute("SELECT * FROM review_dates")}
    for r in store.db.execute("SELECT * FROM reviews ORDER BY fid, sort, page, position"):
        d = dates.get((r["fid"], r["review_id"]))
        observed = d if d is not None and d["observed_at"] else None
        review = merged_review(loads(r["data"]) or {}, r, observed)
        ms = r["list_time_ms"]
        yield {"fid": r["fid"], "sort": r["sort"], "query": r["query"], "page": r["page"], "position": r["position"],
               "fetchedAt": r["fetched_at"], "listSource": r["source"] or "maps",
               # Search's one time (last edit, else publish) and where the exact dates came from
               "listActivityAt": iso_us(ms * 1000) if ms is not None else None,
               "datesSource": d["source"] if observed else None, "datesObservedAt": d["observed_at"] if observed else None,
               "datesStatus": (d["status"] if d is not None else None), "datesAttempts": (d["attempts"] if d is not None else 0),
               "datesError": (d["last_error"] if d is not None else None), **review}
    # partner reviews Google showed (Travel's list, maps/travel_reviews.py): identity = place + source + id;
    # their date is Google's day where the card showed the same review (by its link), otherwise only
    # Google's relative time
    from ..maps.travel_reviews import strip_highlight
    from ..pipeline.review_travel import card_days, partner_day
    positions = {(o["fid"], o["origin"], o["review_id"]): (o["sort"], o["query"], o["position"])
                 for o in store.db.execute("SELECT * FROM travel_order WHERE origin != 'google' ORDER BY position DESC")}
    days: dict[str, dict] = {}
    for p in store.db.execute("SELECT * FROM partner_reviews ORDER BY fid, origin, review_id"):
        sort, query, position = positions.get((p["fid"], p["origin"], p["review_id"]), (p["sort"], p["query"], None))
        if p["fid"] not in days:
            days[p["fid"]] = card_days(store, p["fid"])
        day, basis = partner_day(p, days[p["fid"]])
        data = loads(p["data"]) or {}
        for k in ("text", "textTranslated"):
            if isinstance(data.get(k), str):
                data[k] = strip_highlight(data[k])
        yield {"fid": p["fid"], "sort": sort, "query": query, "page": None, "position": position,
               "fetchedAt": p["fetched_at"], "listSource": "travel", "partnerSourceId": p["source_id"],
               "partnerRating": p["rating"], "partnerScale": p["scale"], "relativeTime": p["relative_time"],
               "datePrecision": "day" if day else "relative", "dateSource": basis,
               "datesSource": None, "datesObservedAt": None, "datesStatus": None, "datesAttempts": 0, "datesError": None,
               **data, "publishedAtDate": day}


def native_images(store: Store) -> Iterator[dict[str, Any]]:
    for r in store.db.execute("SELECT * FROM images ORDER BY fid, position"):
        yield {"fid": r["fid"], "position": r["position"], **(loads(r["data"]) or {})}


def native_contacts(store: Store) -> Iterator[dict[str, Any]]:
    for r in store.db.execute("SELECT * FROM contacts ORDER BY fid, kind"):
        yield {"fid": r["fid"], "kind": r["kind"], "value": r["value"], "domain": r["domain"],
               "sourceUrl": r["source_url"], "ownership": r["ownership"], "context": loads(r["context"])}


def native_discoveries(store: Store) -> Iterator[dict[str, Any]]:
    for r in store.db.execute("SELECT * FROM discoveries ORDER BY id"):
        yield dict(r)


def write_jsonl(rows: Iterator[dict], path: Path) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def export_native(store: Store, out_dir: Path) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    return {
        "places": write_jsonl(native_places(store), out_dir / "places.jsonl"),
        "reviews": write_jsonl(native_reviews(store), out_dir / "reviews.jsonl"),
        "images": write_jsonl(native_images(store), out_dir / "images.jsonl"),
        "contacts": write_jsonl(native_contacts(store), out_dir / "contacts.jsonl"),
        "discoveries": write_jsonl(native_discoveries(store), out_dir / "discoveries.jsonl"),
    }


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(_cell(v) if not isinstance(v, dict) else json.dumps(v, ensure_ascii=False) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


def export_csv(store: Store, out_dir: Path) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    with open(out_dir / "places.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FLAT_PLACE_COLUMNS, extrasaction="ignore")
        w.writeheader()
        n = 0
        for p in native_places(store):
            loc = p.get("location") or {}
            sections = p.get("sections") or {}
            row = {k: _cell(p.get(k)) for k in FLAT_PLACE_COLUMNS}
            row.update(lat=loc.get("lat"), lng=loc.get("lng"),
                       searchKeys=_cell([r["searchKey"] for r in p["results"]]),
                       accepted=any(r["accepted"] for r in p["results"]),
                       filterReasons=_cell([r["filterReason"] for r in p["results"] if r["filterReason"]]),
                       reviewsStatus=(sections.get("reviews") or {}).get("status", ""),
                       reviewsCollected=(sections.get("reviews") or {}).get("count", ""),
                       imagesStatus=(sections.get("images") or {}).get("status", ""),
                       contactsStatus=(sections.get("contacts") or {}).get("status", ""),
                       emails=_cell([c["value"] for c in p["contacts"] if c["kind"] == "email"]),
                       phones=_cell([c["value"] for c in p["contacts"] if c["kind"] == "phone"]),
                       socials=_cell([c["value"] for c in p["contacts"] if c["kind"] not in ("email", "phone",
                                                                                           "phoneUncertain", "impressum")]))
            w.writerow(row)
            n += 1
        counts["places"] = n
    for name, rows, cols in (
            ("reviews", native_reviews(store), ["fid", "reviewId", "publishedAtDate", "stars", "name", "reviewerId",
                                                 "text", "textTranslated", "originalLanguage", "likesCount",
                                                 "responseFromOwnerText", "responseFromOwnerDate", "reviewOrigin",
                                                 "reviewUrl", "sort", "page"]),
            ("images", native_images(store), ["fid", "position", "photoId", "imageUrl", "uploadedAt", "dateEvidence", "authorName",
                                               "authorUrl", "source"]),
            ("contacts", native_contacts(store), ["fid", "kind", "value", "domain", "sourceUrl", "ownership", "context"])):
        with open(out_dir / f"{name}.csv", "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            n = 0
            for r in rows:
                w.writerow({k: _cell(r.get(k)) for k in cols})
                n += 1
            counts[name] = n
    return counts
