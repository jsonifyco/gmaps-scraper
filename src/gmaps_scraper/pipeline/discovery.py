"""Discovery tasks: viewport searches over grid cells, and URL / place-id resolution."""

from __future__ import annotations

import json
import logging
import urllib.parse as up
from typing import Any

from shapely.geometry import Point

from ..geo import grid
from ..maps import pb
from ..maps.client import MapsClient
from ..maps.ids import FID_IN_TEXT_RE, find_fid, normalize_fid
from ..maps.place import PARSER_VERSION as PLACE_PARSER, parse_place
from ..maps.search import parse_search
from ..maps.urls import classify_start_url
from ..maps.xssi import ResponseFormatError
from ..store import loads, now_iso
from ..input_model import ADAPTIVE, OPTIMIZED
from .context import PRIORITY, RunContext
from .filters import reject_reason
from .review_filter import normalize as normalize_phrase

log = logging.getLogger(__name__)

#: offsets fetched per bro command (each page is ~0.4-1.3 MB)
PAGES_PER_CALL = 3
#: Google ends a viewport near 165 places; deeper offsets are never needed
MAX_OFFSET = 400
#: a viewport's list that reached this many places was cut by Google (it ends near 120-170)
VIEWPORT_CAP = 110
#: consecutive pages with (almost) no place inside the cell that end a cell's paging
THIN_PAGES_TO_STOP = 2


class TaskInterrupted(Exception):
    """The run is stopping; the task keeps its progress and returns to the queue."""


class NotReady(Exception):
    """The task depends on work still queued; it goes back to the queue unpenalised."""


class NeedFreshSession(Exception):
    """The task cannot continue on this browser; retry on a new session.

    ``kind`` says why: ``gated`` -- Google refused this session something it serves to others
    (sort, keyword, the reviews tab); ``stalled`` -- the list stopped before its end without a
    refusal (no new pages), which may as well be a reading problem as a Google limit."""

    def __init__(self, message: str, kind: str = "gated") -> None:
        super().__init__(message)
        self.kind = kind


def search_page_url(term: str, lat: float, lng: float, zoom: float, hl: str) -> str:
    path = up.quote(term or "", safe="")
    return f"https://www.google.com/maps/search/{path}/@{lat:.7f},{lng:.7f},{zoom:.2f}z?hl={hl}"


def _viewport(payload: dict) -> tuple[float, float, float, float, grid.Cell | None]:
    if payload.get("cell"):
        cell = grid.Cell.from_json(payload["cell"])
        return cell.lat, cell.lng, cell.altitude, cell.zoom, cell
    v = payload["viewport"]
    alt = pb.altitude_for_zoom(float(v["zoom"]), float(v["lat"]))
    return float(v["lat"]), float(v["lng"]), alt, float(v["zoom"]), None


def handle_search(ctx: RunContext, maps: MapsClient, task: Any) -> dict:
    run = ctx.run
    store = ctx.store
    payload = loads(task["payload"])
    progress = loads(task["progress"]) or {}
    term = payload.get("term") or ""
    search_key = payload["searchKey"]
    lat, lng, alt, zoom, cell = _viewport(payload)
    page_url = search_page_url(term, lat, lng, zoom, ctx.hl)
    limit = run.max_places_per_search
    total_cap = run.legacy.get("maxCrawledPlaces")
    offset = int(progress.get("offset", 0))
    found = int(progress.get("found", 0))
    new_accepted = int(progress.get("new", 0))
    pages = int(progress.get("pages", 0))
    in_cell = int(progress.get("inCell", 0))
    thin_pages = int(progress.get("thinPages", 0))
    unique_fids = set(progress.get("uniqueFids") or [])
    unique_cell_fids = set(progress.get("uniqueCellFids") or [])
    adaptive = run.bro.discoveryAlgorithm in ADAPTIVE
    optimized = run.bro.discoveryAlgorithm in OPTIMIZED
    keep_tail = run.bro.discoveryAlgorithm == "adaptive-v4"
    tail_pages = int(progress.get("tailPages", 0))
    tail_accepted = int(progress.get("tailAccepted", 0))
    mode = run.bro.discoveryFetchMode
    batch_size = (1 if optimized and (mode == "sequential" or
                  (mode == "hybrid" and cell is not None and cell.size_m / 2 < run.bro.minCellKm * 1000))
                  else PAGES_PER_CALL)
    diagnose = ctx.diagnostics("search-fetch", taskId=task["id"]) if optimized else None
    # A stop decided by a page is saved with that page: a retry finishes the cell, never pages on.
    # adaptive-v4 saves with an ``outside_tail`` stop the pages of its batch fetched after it, and a
    # retry reads those still unread first
    pending_tail = list(progress.get("pendingTail") or []) if keep_tail else []
    end_reason = progress.get("stopReason") or ("outside_tail" if pending_tail else None)

    def counters(**extra) -> dict:
        return {"offset": offset, "found": found, "new": new_accepted, "pages": pages, "inCell": in_cell,
                "thinPages": thin_pages, "uniqueFids": sorted(unique_fids), "uniqueCellFids": sorted(unique_cell_fids),
                **({"tailPages": tail_pages, "tailAccepted": tail_accepted} if keep_tail else {}), **extra}

    def read_tail(rest: list[tuple[int, str, Any]]) -> None:
        """adaptive-v4: store the places of batch pages fetched after a spatial stop.  The stop decision
        and these pages were saved with the page that decided the stop; each page read is removed from
        them in its own transaction, so a retry reads only the pages left and the traversal stays that
        of v3.  A page that cannot be read fails the task.  The decision stays saved until the task is
        done: a retry after the last page finishes the cell."""
        nonlocal tail_pages, tail_accepted
        remaining = [{"offset": o, "url": u} for o, u, _ in rest]
        failed = []
        for o, url, resp in rest:
            error = None
            if resp.status != 200 or resp.error:
                error = f"HTTP {resp.status} {resp.error or ''}".strip()
            else:
                try:
                    tail_page = parse_search(resp.text)
                except (ResponseFormatError, ValueError) as exc:
                    error = f"unreadable: {exc}"
            if error:
                ctx.evidence(kind="search_tail", url=url, status=resp.status, text=resp.text, task_id=task["id"],
                             failed=True, note=f"tail page after the stop ({end_reason}) not read: {error}"[:300])
                failed.append(f"offset {o}: {error}")
                continue
            ctx.evidence(kind="search_tail", url=url, status=resp.status, text=resp.text, task_id=task["id"],
                         note=f"fetched after the stop ({end_reason}); places stored, traversal unchanged")
            parsed = [parse_place(item.place, language=ctx.hl, scraped_at=now_iso(), source="search")
                      for item in tail_page.items]
            remaining = [r for r in remaining if r["offset"] != o]
            with store.tx() as db:
                added, _ = _store_page(ctx, db, task, tail_page, term=term, search_key=search_key, offset=o,
                                       page_url=page_url, input_start_url=payload.get("inputStartUrl"),
                                       zoom=zoom, cell=cell, limit=limit, parsed_records=parsed)
                tail_pages += 1
                tail_accepted += added
                store.save_progress(task["id"], counters(stopReason=end_reason, pendingTail=remaining), db)
            if diagnose:
                diagnose({"event": "tail", "url": url, "offset": o, "endReason": end_reason, "added": added})
        if failed:
            raise RuntimeError(f"adaptive-v4: {len(failed)} page(s) fetched after the stop could not be read "
                               f"({'; '.join(failed)})"[:500])
        with store.tx() as db:   # every page read: the stop decision stays, nothing is pending
            store.save_progress(task["id"], counters(stopReason=end_reason), db)

    if pending_tail:
        urls = [r["url"] for r in pending_tail]
        responses = maps.fetch_many(urls)
        if len(responses) != len(urls):
            raise RuntimeError("incomplete search response batch")
        read_tail([(int(r["offset"]), r["url"], resp) for r, resp in zip(pending_tail, responses)])

    while end_reason is None:
        if ctx.should_stop():
            raise TaskInterrupted(ctx.stop_reason or "stop")
        if limit and store.accepted_count(search_key) >= limit:
            end_reason = "limit"
            break
        if total_cap and store.total_accepted() >= total_cap:
            end_reason = "maxCrawledPlaces"
            break
        offsets = [offset + i * pb.SEARCH_PAGE_SIZE for i in range(batch_size)
                   if offset + i * pb.SEARCH_PAGE_SIZE < MAX_OFFSET]
        if not offsets:
            end_reason = "offset_cap"
            break
        urls = [pb.search_url(term, lat, lng, alt, hl=ctx.hl, gl=ctx.gl, offset=o) for o in offsets]
        cached = {u: ctx.search_cache.get(u, ctx.hl, ctx.gl) for u in urls} if optimized else {}
        missing = [u for u in urls if cached.get(u) is None]
        fetched = maps.fetch_many(missing) if missing else []
        if len(fetched) != len(missing):
            raise RuntimeError("incomplete search response batch")
        fresh = dict(zip(missing, fetched))
        responses = [cached.get(u) or fresh[u] for u in urls]
        if diagnose:
            diagnose({"event": "fetched", "mode": mode, "requests": [
                {"url": u, "cached": cached.get(u) is not None, "status": r.status,
                 "bytes": len((r.text or "").encode("utf-8")), "elapsedSeconds": r.elapsed}
                for u, r in zip(urls, responses)]})
        processed = 0
        for o, url, resp in zip(offsets, urls, responses):
            if resp.status != 200 or resp.error:
                ctx.evidence(kind="search", url=url, status=resp.status, text=resp.text, task_id=task["id"],
                             note=resp.error, failed=True)
                raise RuntimeError(f"search page failed: HTTP {resp.status} {resp.error or ''}"[:300])
            try:
                page = parse_search(resp.text)
            except (ResponseFormatError, ValueError) as exc:
                ctx.evidence(kind="search", url=url, status=resp.status, text=resp.text, task_id=task["id"],
                             note=str(exc), failed=True)
                raise RuntimeError(f"unreadable search page: {exc}") from exc
            ctx.evidence(kind="search", url=url, status=resp.status, text=resp.text, task_id=task["id"])
            parsed = ([parse_place(item.place, language=ctx.hl, scraped_at=now_iso(), source="search")
                       for item in page.items] if optimized else None)
            if optimized:
                ctx.search_cache.put(resp, ctx.hl, ctx.gl)
                diagnose({"event": "processed", "url": url, "offset": o, "rows": len(page.items)})
            with store.tx() as db:
                added, page_in_cell = _store_page(ctx, db, task, page, term=term, search_key=search_key, offset=o,
                                                  page_url=page_url, input_start_url=payload.get("inputStartUrl"),
                                                  zoom=zoom, cell=cell, limit=limit, **({"parsed_records": parsed} if optimized else {}))
                pages += 1
                found += len(page.items)
                if adaptive:
                    for item_index, item in enumerate(page.items):
                        fields, _ = parsed[item_index] if parsed is not None else parse_place(item.place, language=ctx.hl, scraped_at=now_iso(), source="search")
                        if fields.get("fid"):
                            unique_fids.add(fields["fid"])
                            loc = fields.get("location") or {}
                            if cell and loc.get("lat") is not None and loc.get("lng") is not None and cell.polygon(margin=.05).covers(Point(loc["lng"], loc["lat"])):
                                unique_cell_fids.add(fields["fid"])
                in_cell += page_in_cell
                new_accepted += added
                offset = o + pb.SEARCH_PAGE_SIZE
                if page.empty or "single_place_answer" in page.notes:
                    end_reason = "single_place" if page.notes else "viewport_end"
                elif cell is not None:
                    # Google pads a viewport with places around it; once pages bring (almost)
                    # nothing inside the cell, the rest belongs to neighbouring cells
                    thin_pages = thin_pages + 1 if page_in_cell < max(1, len(page.items) // 10) else 0
                    if thin_pages >= THIN_PAGES_TO_STOP:
                        end_reason = "outside_tail"
                stop = {"stopReason": end_reason} if end_reason else {}
                tail = [{"offset": po, "url": pu} for po, pu in zip(offsets, urls)][processed + 1:]
                if keep_tail and end_reason == "outside_tail" and tail:
                    stop["pendingTail"] = tail
                store.save_progress(task["id"], counters(**stop), db)
            processed += 1
            if end_reason:
                break
            if limit and store.accepted_count(search_key) >= limit:
                end_reason = "limit"
                break
            if total_cap and store.total_accepted() >= total_cap:
                end_reason = "maxCrawledPlaces"
                break
        # pages of the batch fetched after the stop.  After the spatial stop (outside_tail) adaptive-v4
        # must read them (they held the places v3 lost): read_tail stores their places or fails the task.
        # After any other stop -- the list ended, a limit was reached -- they hold nothing required and
        # are kept with evidence=all only, as by the other strategies, for diagnosing what was left out
        rest = list(zip(offsets, urls, responses))[processed:]
        if keep_tail and end_reason == "outside_tail" and rest:
            read_tail(rest)
            continue
        for o, url, resp in rest:
            ctx.evidence(kind="search_unprocessed", url=url, status=resp.status, text=resp.text,
                         task_id=task["id"], note=f"fetched after the stop ({end_reason}); not processed")
            if diagnose:
                diagnose({"event": "unprocessed", "url": url, "offset": o, "endReason": end_reason})

    split = False
    # Google cuts a viewport's list near VIEWPORT_CAP places; when the list reached the cap and a good
    # share of it lies inside the cell, lower-ranked places of the cell may be missing: split it
    capped = (len(unique_fids) if adaptive else found) >= VIEWPORT_CAP or end_reason == "offset_cap"
    suspect_tail = adaptive and end_reason == "outside_tail"
    if (cell is not None and payload.get("subdivide") and end_reason in ("viewport_end", "offset_cap", "outside_tail")
            and (suspect_tail or (capped and (len(unique_cell_fids) if adaptive else in_cell) >= run.bro.saturationThreshold))):
        if cell.size_m / 2 >= run.bro.minCellKm * 1000:
            terr = next((t for t in ctx.territories if t.id == payload.get("territoryId")), None)
            children = grid.children_inside(cell, terr.area) if terr else cell.children()
            with store.tx() as db:
                for child in children:
                    store.add_task("search", f"{search_key}|t{payload.get('territoryId')}|{child.key}",
                                   {**payload, "cell": child.to_json()}, priority=PRIORITY["search"],
                                   parent_id=task["id"], db=db)
            split = True
            end_reason = "outside_tail_split" if suspect_tail else "saturated_split"
        else:
            end_reason = "outside_tail_min_size" if suspect_tail else "saturated_min_size"
    return {"pages": pages, "found": found, "inCell": in_cell, "newAccepted": new_accepted, "endReason": end_reason,
            "split": split, "cell": payload.get("cell"), "searchKey": search_key, "term": term,
            "uniqueFound": len(unique_fids) if adaptive else None,
            "uniqueInCell": len(unique_cell_fids) if adaptive else None,
            "tailPages": tail_pages if keep_tail else None, "tailAccepted": tail_accepted if keep_tail else None,
            "algorithm": run.bro.discoveryAlgorithm}


def _keep_query_neutral_categories(store, db, fid: str, fields: dict, term: str | None) -> None:
    """A search list puts the category matching the query first: a "bar" search lists a New
    American restaurant that also has "Bar" as ["Bar", "American restaurant", "New American
    restaurant", ...], while its place page (and an unrelated query) has "New American restaurant"
    first (3 of 3 places checked against rendered pages, 2026-10-08).  The primary's original
    position is not recoverable from such a list, so the order of a record whose first category is
    not the query is kept when the place was (or is later) found by such a query."""
    cats = fields.get("categories") or []
    fronted = bool(term) and len(cats) > 1 and cats[0].strip().lower() == term.strip().lower()
    row = db.execute("SELECT search_data FROM places WHERE fid = ?", (fid,)).fetchone()
    old = loads(row[0]) if row and row[0] else None
    if fronted and old and old.get("categories") and not old.get("categoriesQueryFronted"):
        fields["categories"], fields["categoryName"] = old["categories"], old.get("categoryName")
    else:
        fields["categoriesQueryFronted"] = fronted


def _store_page(ctx: RunContext, db, task, page, *, term: str, search_key: str, offset: int, page_url: str,
                input_start_url: str | None, zoom: float, cell: grid.Cell | None,
                limit: int | None, parsed_records: list | None = None) -> tuple[int, int]:
    """Persist one search page; return (places newly in this search's output, places inside the cell)."""
    store = ctx.store
    run = ctx.run
    added = 0
    in_cell = 0
    cell_poly = cell.polygon(margin=0.05) if cell is not None else None
    now = now_iso()
    for item_index, item in enumerate(page.items):
        fields, errors = parsed_records[item_index] if parsed_records is not None else parse_place(item.place, language=ctx.hl, scraped_at=now_iso(), source="search")
        fid = fields.get("fid")
        if not fid:
            continue
        _keep_query_neutral_categories(store, db, fid, fields, term)
        store.upsert_place(db, fid, fields, source="search", language=ctx.hl, parser_version=PLACE_PARSER,
                           errors=errors)
        disc_id = store.add_discovery(db, fid=fid, source="search", search_term=term, task_id=task["id"],
                                      cell_key=cell.key if cell else None, cell_pass=cell.pass_name if cell else None,
                                      zoom=zoom, page=offset // pb.SEARCH_PAGE_SIZE, position=item.position,
                                      rank=offset + item.position + 1, is_ad=int(item.is_ad), search_page_url=page_url,
                                      input_start_url=input_start_url)
        loc = fields.get("location") or {}
        if cell_poly is not None and loc.get("lat") is not None and cell_poly.covers(Point(loc["lng"], loc["lat"])):
            in_cell += 1
        inside = ctx.inside(loc.get("lat"), loc.get("lng"))
        reason = None
        if inside is False:
            reason = "outside_area"
        elif inside is None:
            reason = "unverified_location"
        else:
            reason = reject_reason(fields, run, search_term=term, from_search=True)
        if reason is None and limit and store.accepted_count(search_key, db) >= limit:
            reason = "limit_reached"
        total_cap = run.legacy.get("maxCrawledPlaces")
        if reason is None and total_cap:
            # The page is committed under Store.tx (BEGIN IMMEDIATE). Check each new
            # global identity here: several workers may already have fetched a page.
            known = db.execute("SELECT 1 FROM results WHERE fid=? AND accepted=1 LIMIT 1", (fid,)).fetchone()
            if known is None and store.total_accepted(db) >= total_cap:
                reason = "total_limit_reached"
        exists = db.execute("SELECT accepted FROM results WHERE fid = ? AND search_key = ?", (fid, search_key)).fetchone()
        if exists is not None:
            continue
        rank = db.execute("SELECT COUNT(*) FROM results WHERE search_key = ?", (search_key,)).fetchone()[0] + 1
        store.add_result(db, fid=fid, search_key=search_key, search_term=term, rank=rank,
                         accepted=0 if reason else 1, filter_reason=reason,
                         inside_area=None if inside is None else int(inside), input_start_url=input_start_url,
                         search_page_url=page_url, first_discovery_id=disc_id)
        if reason is None:
            added += 1
            if run.details_enabled:
                store.add_task("place", fid, {"fid": fid}, priority=PRIORITY["place"], db=db)
            else:
                schedule_followups(ctx, db, fid, fields)
    return added, in_cell


def schedule_followups(ctx: RunContext, db, fid: str, fields: dict) -> None:
    """Sections that depend on an accepted place (reviews, images, website, extras)."""
    run = ctx.run
    a = run.apify
    store = ctx.store
    reviews_limit = run.reviews_limit
    if reviews_limit is None or reviews_limit > 0:
        if fields.get("reviewsCount") == 0:
            store.set_section(fid, "reviews", "done", reason="no_reviews", count=0, expected=0, db=db)
        else:
            # a filtered list counts matching reviews: not measured against the place's review count
            filtered = bool(normalize_phrase(a.get("reviewsFilterString")))
            store.set_section(fid, "reviews", "pending", expected=None if filtered else fields.get("reviewsCount"),
                              db=db)
            store.add_task("reviews", f"{fid}|{a['reviewsSort']}|{a.get('reviewsFilterString') or ''}",
                           {"fid": fid, "sort": a["reviewsSort"], "query": a.get("reviewsFilterString") or None,
                            "limit": reviews_limit, "expected": fields.get("reviewsCount")},
                           priority=PRIORITY["reviews"], db=db)
    else:
        store.set_section(fid, "reviews", "not_requested", db=db)
    images_limit = run.images_limit
    if images_limit is None or images_limit > 0:
        store.set_section(fid, "images", "pending", expected=fields.get("imagesCount"), db=db)
        store.add_task("images", fid, {"fid": fid, "limit": images_limit, "authors": bool(a["scrapeImageAuthors"])},
                       priority=PRIORITY["images"], db=db)
    else:
        store.set_section(fid, "images", "not_requested", db=db)
    if a["scrapeContacts"]:
        website = fields.get("website")
        if website:
            domain = website_domain(website)
            store.set_section(fid, "contacts", "pending", detail={"domain": domain}, db=db)
            # bro.websiteEarly: right after the review lists, before the date tasks, so that a slow site does not
            # start only once every other section is done
            prio = PRIORITY["website_early"] if run.bro.websiteEarly else PRIORITY["website"]
            if store.add_task("website", domain, {"domain": domain, "url": website}, priority=prio, db=db) is None:
                # the domain's task exists (another place with the same website): take its stored result,
                # or be attributed when it is stored (rc26 review)
                from ..enrich.website import attach_late_place
                attach_late_place(ctx, db, fid, domain)
        else:
            store.set_section(fid, "contacts", "absent", reason="no_website", db=db)
    else:
        store.set_section(fid, "contacts", "not_requested", db=db)
    if a["includeWebResults"]:
        store.set_section(fid, "webResults", "pending", db=db)
        store.add_task("webresults", fid, {"fid": fid}, priority=PRIORITY["webresults"], db=db)
    if a["maxQuestions"]:
        from .questions import schedule_questions
        schedule_questions(ctx, db, fid)
    from .specials import schedule_specials
    schedule_specials(ctx, db, fid, fields)


def website_domain(url: str) -> str:
    host = up.urlparse(url if "://" in url else "http://" + url).netloc.lower()
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


# ------------------------------------------------------------------ resolve
def handle_resolve(ctx: RunContext, maps: MapsClient, task: Any) -> dict:
    """Open a URL Google has to resolve (short links, lists, names, opaque place ids)."""
    payload = loads(task["payload"])
    store = ctx.store
    kind = payload["kind"]
    search_key = payload["searchKey"]
    if kind == "placeId":
        url = f"https://www.google.com/maps/place/?q=place_id:{up.quote(payload['placeId'])}&hl={ctx.hl}&gl={ctx.gl}"
    else:
        url = payload["url"]
    maps.ensure_maps()
    final = maps.open(url, settle=2.0)
    if kind == "list":
        return _resolve_list(ctx, maps, task, search_key, payload["url"])
    target = classify_start_url(final)
    fid = target.fid or find_fid(final)
    if not fid:
        html_fids = maps.js("(() => { const m = (document.documentElement.innerHTML.match("
                            "/0x[0-9a-f]{6,16}:0x[0-9a-f]{6,16}/g) || []); return Array.from(new Set(m)).slice(0, 5); })()",
                            out_type="list") or []
        fid = normalize_fid(html_fids[0]) if len(html_fids) == 1 else None
    with store.tx() as db:
        if fid:
            store.add_result(db, fid=normalize_fid(fid), search_key=search_key, search_term=None, rank=None, accepted=None,
                             input_start_url=payload.get("url"), input_place_id=payload.get("placeId"))
            store.add_task("place", normalize_fid(fid), {"fid": normalize_fid(fid)}, priority=PRIORITY["place"], db=db)
            return {"resolved": fid, "finalUrl": final}
        if target.kind == "search":
            view = ({"lat": target.lat, "lng": target.lng, "zoom": target.zoom or 13.0} if target.lat is not None
                    else {"lat": 30.0, "lng": 0.0, "zoom": 3.0})
            store.add_task("search", f"{search_key}|resolved", {"term": target.query or "", "searchKey": search_key,
                                                                "viewport": view, "subdivide": False,
                                                                "inputStartUrl": payload.get("url")},
                           priority=PRIORITY["search"], db=db)
            return {"resolvedSearch": target.query, "finalUrl": final}
    raise RuntimeError(f"could not resolve {url[:200]} (landed on {final[:200]})")


def _resolve_list(ctx: RunContext, maps: MapsClient, task: Any, search_key: str, url: str) -> dict:
    """A saved list: every fid the list page carries becomes a requested place."""
    fids = maps.js("(() => { const m = (document.documentElement.innerHTML.match("
                   "/0x[0-9a-f]{6,16}:0x[0-9a-f]{6,16}/g) || []); return Array.from(new Set(m)); })()",
                   out_type="list") or []
    fids = [normalize_fid(f) for f in fids if not f.startswith("0x0:")]
    with ctx.store.tx() as db:
        for fid in fids:
            ctx.store.add_result(db, fid=fid, search_key=search_key, search_term=None, rank=None, accepted=None,
                                 input_start_url=url)
            ctx.store.add_task("place", fid, {"fid": fid}, priority=PRIORITY["place"], db=db)
    if not fids:
        raise RuntimeError(f"no places found on list page {url[:200]}")
    return {"listPlaces": len(fids)}


# ------------------------------------------------------------- coverage passes
EXTENDED_MAX_SYNONYMS = 5


def handle_controlpass(ctx: RunContext, maps: MapsClient, task: Any) -> dict:
    """After the main pass of a term: search the offset grid (deep), then related categories (extended).

    Runs without the browser.  It waits (``NotReady``) while main-pass cells of
    the same term and territory are still queued.  The offset cells are the
    finest searched cells moved by half a cell; places they find that the main
    pass missed show up as the ``offset`` gain in the coverage report.
    """
    store = ctx.store
    p = loads(task["payload"])
    key, terr_id = p["searchKey"], p["territoryId"]
    prefix = f"{key}|t{terr_id}|main:"
    busy = store.db.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'search' AND key LIKE ? AND status IN "
                            "('pending', 'running')", (prefix + "%",)).fetchone()[0]
    if busy:
        raise NotReady(f"{busy} main-pass cell(s) of {key} still queued")
    terr = next((t for t in ctx.territories if t.id == terr_id), None)
    if terr is None:
        return {"skipped": "territory unknown"}
    leaves = []
    for row in store.db.execute("SELECT payload, result FROM tasks WHERE kind = 'search' AND key LIKE ? AND status = 'done'",
                                (prefix + "%",)):
        result = loads(row["result"]) or {}
        if result.get("endReason") not in ("saturated_split", "outside_tail_split"):
            leaves.append(grid.Cell.from_json(loads(row["payload"])["cell"]))
    added = 0
    synonyms: list[str] = []
    with store.tx() as db:
        for cell in grid.offset_pass(leaves, terr.area):
            if store.add_task("search", f"{key}|t{terr_id}|{cell.key}",
                              {"term": p["term"], "searchKey": key, "territoryId": terr_id, "cell": cell.to_json(),
                               "subdivide": ctx.run.bro.discoveryAlgorithm in ADAPTIVE}, priority=PRIORITY["search"],
                              parent_id=task["id"], db=db):
                added += 1
        if p.get("coverage") == "extended":
            synonyms = _related_categories(db, key, p["term"])
            for syn in synonyms:
                ext_key = f"ext:{p['term']}:{syn}"
                for cell in leaves:
                    store.add_task("search", f"{ext_key}|t{terr_id}|{cell.key}",
                                   {"term": syn, "searchKey": ext_key, "territoryId": terr_id, "cell": cell.to_json(),
                                    "subdivide": False, "extendedFrom": p["term"]}, priority=PRIORITY["search"] + 1,
                                   parent_id=task["id"], db=db)
    return {"leaves": len(leaves), "offsetCells": added, "extendedTerms": synonyms}


def _related_categories(db, key: str, term: str) -> list[str]:
    """Most frequent primary categories among accepted places of ``key``, other than the term itself."""
    counts: dict[str, int] = {}
    for (data,) in db.execute("SELECT p.search_data FROM results r JOIN places p ON p.fid = r.fid "
                              "WHERE r.search_key = ? AND r.accepted = 1", (key,)):
        cat = (loads(data) or {}).get("categoryName")
        if cat and cat.casefold() != term.casefold():
            counts[cat] = counts.get(cat, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [c for c, n in ranked[:EXTENDED_MAX_SYNONYMS] if n >= 2]
