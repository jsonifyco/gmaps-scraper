"""Google's Travel review list in the review route (collection contract ``core-2026.10``).

Search stays the source of Google review records (every field, Search's list time, Maps dates
read from their single-review pages).  Travel's list (maps/travel_reviews.py) adds what Search
does not carry, by one of four passes chosen per list (:func:`travel_mode`):

* ``partners`` -- newest, ``reviewsOrigin`` all, and the card names partner sources: after the
  Search list, Travel's newest list gives the partner reviews and their places among Google's
  (Google's part of it equals Search's list in the same order);
* ``relevant`` -- most relevant with partner sources: Travel's relevance list is the list (Search
  ranks a hotel's reviews otherwise), then Search records for its Google ids;
* ``filter`` -- ``reviewsFilterString`` with ``bro.reviewsFilterMode`` google: Travel's search
  gives Google's matches in Google's order, then Search records are looked up for its Google ids;
* ``rating`` -- highest / lowest with ``bro.ratingSortMode`` google: Travel's rating selection
  (longer than Search's, partners at their rating), then Search records for its Google ids;
* ``complete`` -- highest / lowest with ``bro.ratingSortMode`` complete: Travel's complete most
  relevant list, sorted by rating at export (ties in Google's relevance order), and only called
  complete when Google's part reaches the place's count.

Every Travel page is stored with its cursor in one transaction (store.add_travel_page), and the
task's progress keeps the phase and cursors, so a stop or a lost session continues where it was.
The internal identity of a review is place + source + id (partner records in ``partner_reviews``);
a public id never changes.

The limit applies to the combined list as exported: with ``reviewsStartDate`` a record the export
drops (a Google review older by the time Google shows, a partner review older by its day) does not
count, and the list is read and looked up further until ``maxReviews`` records are settled
(:func:`settled_count`).  A partner review gets the card's day only when the card's sample is the
same review by its link (:func:`card_days`); otherwise it keeps Google's relative time.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Callable

from ..maps.search_reviews import (PARSER_VERSION as SEARCH_PARSER, SearchRefused, SearchReviewsLister,
                                   SearchUnreadable, list_time_ms, owner_hash, parse_search_review)
from ..maps.travel_reviews import (GOOGLE_SOURCE, PAGE_SIZE, PARSER_VERSION as TRAVEL_PARSER, TravelRecord,
                                   TravelRefused, TravelReviewsLister, TravelUnreadable, google_stub,
                                   maps_review_url, partner_review, partner_review_ref, stable_key,
                                   strip_highlight)
from ..store import dumps, loads, now_iso
from .discovery import NeedFreshSession, TaskInterrupted
from .review_dates import export_rows

log = logging.getLogger(__name__)

RATING_SORTS = ("highestRanking", "lowestRanking")


class TravelIdentityError(RuntimeError):
    """Travel's list does not belong to the place (its Google reviews are not the place's)."""


def list_key(run: Any, raw_query: str | None) -> str:
    """The key a list is stored under: '' (no filter), the normalised phrase (local filter) or
    'google:<phrase>' (Google's review search)."""
    from .review_filter import normalize
    phrase = normalize(raw_query)
    if not phrase:
        return ""
    if run.bro.core_contract and run.bro.reviewsFilterMode == "google":
        return "google:" + " ".join(str(raw_query).split())
    return phrase


def google_query(key: str) -> str:
    return key[len("google:"):] if key.startswith("google:") else ""


def card_partner_sources(store: Any, fid: str) -> list[dict]:
    place = store.place(fid)
    if place is None:
        return []
    details = loads(place["details"]) or {}
    return [s for s in details.get("partnerReviewSources") or [] if isinstance(s, dict)]


def travel_mode(run: Any, store: Any, fid: str, sort: str, key: str) -> str | None:
    """The Travel pass this list needs under the job's contract (None = Search only, as rc27)."""
    if not run.bro.core_contract:
        return None
    if key.startswith("google:"):
        return "filter"
    if key:
        return None  # the local filter reads Search's whole list (rc27 behaviour, chosen explicitly)
    if sort in RATING_SORTS:
        return "complete" if run.bro.ratingSortMode == "complete" else "rating"
    if run.apify.get("reviewsOrigin") != "google" and card_partner_sources(store, fid):
        # newest: Google's part of Travel's list is Search's list in the same order, so Travel only places
        # the partners; most relevant: Search ranks a hotel's reviews otherwise (Apify's order = Travel's,
        # 30/30 with 0 inversions on 3 hotels, rc29 acceptance), so Travel's order is the list
        return "partners" if sort == "newest" else "relevant"
    return None


def travel_sources(run: Any) -> list[int] | None:
    return [GOOGLE_SOURCE] if run.apify.get("reviewsOrigin") == "google" else None


def travel_read_order(mode: str, sort: str) -> str:
    return "mostRelevant" if mode == "complete" else sort


def card_days(store: Any, fid: str) -> dict[tuple[int, str], str]:
    """Google's day of the partner reviews the card showed (one sample per source), keyed by source +
    the review's identity in its link (maps/travel_reviews.partner_review_ref).  A sample whose link
    names only the hotel (Trip.com, Priceline) is tied to no review, so its day goes to none: an
    author's display name or a matching text is not the identity of a publication."""
    out = {}
    for src in card_partner_sources(store, fid):
        sample = src.get("sample") or {}
        ref = partner_review_ref(src.get("sourceId"), sample.get("url"))
        if sample.get("dayDate") and ref:
            out[(src["sourceId"], ref)] = sample["dayDate"]
    return out


def partner_day(row: Any, days: dict) -> tuple[str | None, str | None]:
    """A stored partner review's day and its basis.  A day matched by link (``card_link``) stays; rows
    read before rc32 were matched by author and text (``card_sample``) and keep their day only when the
    card's sample is the same review by its link."""
    if not row["day_date"]:
        return None, None
    if row["day_source"] == "card_link":
        return row["day_date"], "card_link"
    if days.get((row["source_id"], partner_review_ref(row["source_id"], row["partner_url"]))) == row["day_date"]:
        return row["day_date"], "card_link"
    return None, None


def keep_partner(review: dict, start: datetime | None) -> bool:
    """A partner review leaves a list with reviewsStartDate only by Google's day before the date;
    without a day it is kept (a relative time is not a moment)."""
    day = review.get("publishedAtDate")
    return start is None or not day or datetime.fromisoformat(day.replace("Z", "+00:00")) >= start


def _entries(records: list[TravelRecord], days: dict, *, fid: str, hl: str, personal: bool) -> list[dict]:
    out = []
    for r in records:
        if r.is_google:
            out.append({"origin": "google", "reviewId": r.review_id, "stub": google_stub(r, fid, hl=hl, personal_data=personal)})
            continue
        rid = r.review_id or stable_key(r)
        day = days.get((r.source_id, partner_review_ref(r.source_id, r.partner_url)))
        out.append({"origin": r.source, "reviewId": rid, "sourceId": r.source_id, "rating": r.rating, "scale": r.scale,
                    "relative": r.relative, "partnerUrl": r.partner_url, "dayDate": day,
                    "daySource": "card_link" if day else None,
                    "review": partner_review(r, day_date=day, personal_data=personal, hl=hl)})
    return out


def check_identity(store: Any, fid: str, records: list[TravelRecord], *, min_search: int = 20) -> None:
    """Travel's Google records must be the place's: with enough of the place's Search list stored, a
    page of Google records sharing none of it is another place's list."""
    gids = [r.review_id for r in records if r.is_google]
    if len(gids) < 5:
        return
    known = {row[0] for row in store.db.execute("SELECT review_id FROM reviews WHERE fid = ? AND source = 'search'", (fid,))}
    if len(known) >= min_search and not known.intersection(gids):
        raise TravelIdentityError(f"Travel's list for {fid}: none of {len(gids)} Google reviews is in the place's "
                                  f"Search list ({len(known)} stored)")


def read_travel(ctx: Any, maps: Any, task: Any, progress: dict, *, fid: str, sort: str, key: str, mode: str,
                want: int | None, cover: set[str] | None = None, settled: Callable[[], int] | None = None) -> dict:
    """Page Travel's list until ``want`` records are stored -- with ``settled``, until that many records
    of the export are settled -- (None = to its end) or, when ``cover`` is given, every id of it is
    listed; progress keys ``t*``; returns the progress."""
    store, run = ctx.store, ctx.run
    personal = bool(run.apify.get("scrapeReviewsPersonalData", True))
    lister = TravelReviewsLister(maps, fid, sort=travel_read_order(mode, sort), query=google_query(key),
                                 sources=travel_sources(run))
    home = SearchReviewsLister(maps, fid).ensure_home
    days = card_days(store, fid)
    while not progress.get("tEnded"):
        stored = store.db.execute("SELECT COUNT(*) FROM travel_order WHERE fid = ? AND sort = ? AND query = ?",
                                  (fid, sort, key)).fetchone()[0]
        have = stored if settled is None or want is None else settled()
        if want is not None and have >= want:
            break
        if cover is not None and want is None:
            listed = {r[0] for r in store.db.execute("SELECT review_id FROM travel_order WHERE fid = ? AND sort = ? AND "
                                                     "query = ? AND origin = 'google'", (fid, sort, key))}
            if cover <= listed:
                break
        if ctx.should_stop():
            raise TaskInterrupted(f"run_{ctx.stop_reason or 'stop'}")
        page_no = int(progress.get("tPage", 0)) + 1
        cursor = progress.get("tCursor")
        if want is not None:
            lister.page_size = max(20, min(PAGE_SIZE, want - have))
        label = f"ocp93e:{fid}:{lister.order}:{lister.query}:{page_no}"
        try:
            page = lister.page(cursor, home=home)
        except TravelRefused as exc:
            raise NeedFreshSession(f"Travel refused the review list: {exc}") from exc
        except TravelUnreadable as exc:
            ctx.evidence(kind="reviews_travel", url=label, status=200, text=exc.raw or str(exc), task_id=task["id"],
                         note=str(exc)[:300], failed=True)
            raise RuntimeError(f"unreadable Travel review answer: {exc}") from exc
        ctx.evidence(kind="reviews_travel", url=label, status=200, text=page.raw, task_id=task["id"])
        if mode == "partners":
            try:
                check_identity(store, fid, page.records)
            except TravelIdentityError:
                ctx.evidence(kind="reviews_travel", url=label, status=200, text=page.raw, task_id=task["id"],
                             note="identity", failed=True)
                raise
        if cursor and page.next_cursor == cursor:
            raise RuntimeError(f"Travel's cursor did not advance on page {page_no} of {fid}")
        entries = _entries(page.records, days, fid=fid, hl=ctx.hl, personal=personal)
        with store.tx() as db:
            store.add_travel_page(db, fid, sort, key, page_no, cursor, page.next_cursor, entries, TRAVEL_PARSER,
                                  repeated=page.repeated)
            progress.update(tPage=page_no, tCursor=page.next_cursor, tEnded=page.next_cursor is None)
            store.save_progress(task["id"], progress, db)
    return progress


def _before(row: dict, start_ms: int | None) -> bool:
    """A Search row whose time (the last edit, else the publish time: the time Google shows) is before
    reviewsStartDate: the export drops it, so it is not stored under the list."""
    ms = row.get("listTimeMs")
    return start_ms is not None and ms is not None and ms < start_ms


def hydrate(ctx: Any, maps: Any, task: Any, progress: dict, *, fid: str, sort: str, key: str, mode: str,
            needed: list[str], start_ms: int | None = None) -> dict:
    """Search records for the Google ids Travel selected: Search's list of the same order first (it
    shares the head of a rating selection), then the newest list (complete) until every id is
    found.  Only those reviews are stored (sort + key of the job's list), without the ones before
    reviewsStartDate; every record of a page read here is pooled (store.search_pool), so ids needed
    later -- a list read on for the start date -- are found without reading Search again.
    Progress keys ``h*``."""
    store, run = ctx.store, ctx.run
    personal = bool(run.apify.get("scrapeReviewsPersonalData", True))
    plan = ([sort, "newest"] if mode in ("rating", "relevant") and sort != "newest" else
            ["newest" if mode == "filter" else travel_read_order(mode, sort)])
    want = set(needed)
    found = {r[0] for r in store.db.execute("SELECT review_id FROM reviews WHERE fid = ? AND sort = ? AND query = ? "
                                            "AND source = 'search'", (fid, sort, key))}
    pooled = [r for r in store.pool_rows(fid, want - found).values() if not _before(r, start_ms)]
    if pooled:
        with store.tx() as db:
            store.put_search_rows(db, fid, sort, key, 99999, pooled, SEARCH_PARSER)
    while int(progress.get("hList", 0)) < len(plan):
        found = {r[0] for r in store.db.execute("SELECT review_id FROM reviews WHERE fid = ? AND sort = ? AND query = ? "
                                                "AND source = 'search'", (fid, sort, key))}
        # an id is found when it is stored, or Search showed it with a time before the start date
        if want <= found | set(store.pool_rows(fid, want - found)):
            break
        order = plan[int(progress.get("hList", 0))]
        lister = SearchReviewsLister(maps, fid, sort=order)
        if ctx.should_stop():
            raise TaskInterrupted(f"run_{ctx.stop_reason or 'stop'}")
        page_no = int(progress.get("hPage", 0)) + 1
        cursor = progress.get("hCursor")
        try:
            page = lister.page(cursor)
        except SearchRefused as exc:
            raise NeedFreshSession(f"Search refused the review list: {exc}") from exc
        except SearchUnreadable as exc:
            ctx.evidence(kind="reviews_search", url=f"GetLocalBoqProxy:{fid}:{order}:{page_no}", status=200,
                         text=exc.raw or str(exc), task_id=task["id"], note=str(exc)[:300], failed=True)
            raise RuntimeError(f"unreadable Search review answer: {exc}") from exc
        ctx.evidence(kind="reviews_search", url=f"GetLocalBoqProxy:{fid}:{order}:{page_no}", status=200,
                     text=page.raw, task_id=task["id"])
        seen = []
        for rec in page.records:
            review = parse_search_review(rec, hl=ctx.hl, personal_data=personal)
            seen.append({"review": review, "listTimeMs": list_time_ms(rec),
                         "ownerHash": owner_hash(review.get("responseFromOwnerText")),
                         "reviewUrl": parse_search_review(rec, hl=ctx.hl)["reviewUrl"], "origin": review.get("reviewOrigin")})
        rows = [r for r in seen if r["review"]["reviewId"] in want and not _before(r, start_ms)]
        with store.tx() as db:
            store.add_pool_rows(db, fid, order, page_no, seen, SEARCH_PARSER)
            # stored under the job's list, at the Travel position (page/position follow the order table)
            store.add_search_page(db, fid, sort, key, 100000 + int(progress.get("hList", 0)) * 10000 + page_no,
                                  cursor, page.next_cursor, rows, SEARCH_PARSER)
            if page.next_cursor is None:
                progress.update(hList=int(progress.get("hList", 0)) + 1, hPage=0, hCursor=None)
            else:
                progress.update(hPage=page_no, hCursor=page.next_cursor)
            store.save_progress(task["id"], progress, db)
    return progress


def store_stubs(store: Any, fid: str, sort: str, key: str, ids: list[str], *, hl: str) -> int:
    """Google ids Travel selected (``ids``: the part of its list looked up) that no Search list showed:
    kept with Travel's fields (source 'travel': author, text, stars, relative time), so the export
    keeps Google's selection.  Their Maps single-review page can still give the exact dates: the link
    it needs is kept internally (``reviews.review_url``) also when scrapeReviewsPersonalData leaves it
    out of the review, as for Search's reviews.  An id Search showed with a time before
    reviewsStartDate (pooled, not stored) is no stub.  Returns the stubs of the list."""
    want = set(ids)
    found = {r[0] for r in store.db.execute("SELECT review_id FROM reviews WHERE fid = ?", (fid,))}
    pooled = set(store.pool_rows(fid, want - found))
    missing = [r for r in store.travel_order(fid, sort, key)
               if r["origin"] == "google" and r["review_id"] in want and r["review_id"] not in found | pooled]
    with store.tx() as db:
        for row in missing:
            stub = loads(row["data"]) or {"reviewId": row["review_id"], "reviewOrigin": "Google"}
            db.execute("INSERT OR IGNORE INTO reviews (fid, review_id, data, stars, sort, query, page, position, fetched_at, "
                       "parser_version, attempt, source, review_url, origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'travel', ?, "
                       "'Google')", (fid, row["review_id"], dumps(stub), stub.get("stars"), sort, key, 900000, row["position"],
                                     now_iso(), TRAVEL_PARSER, maps_review_url(row["review_id"], fid, hl=hl)))
        # stubs stored before rc32 without the link (personal data off) get it for their date read
        for (rid,) in db.execute("SELECT review_id FROM reviews WHERE fid = ? AND sort = ? AND query = ? AND source = 'travel' "
                                 "AND review_url IS NULL", (fid, sort, key)).fetchall():
            db.execute("UPDATE reviews SET review_url = ? WHERE fid = ? AND review_id = ?",
                       (maps_review_url(rid, fid, hl=hl), fid, rid))
        return db.execute("SELECT COUNT(*) FROM reviews WHERE fid = ? AND sort = ? AND query = ? AND source = 'travel'",
                          (fid, sort, key)).fetchone()[0]


# ---------------------------------------------------------------------------------------- export
def _ratio(value: Any, scale: Any) -> float | None:
    try:
        return float(value) / float(scale)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _stars(review: dict) -> float | None:
    return _ratio(review.get("stars"), 5)


def _walk(store: Any, fid: str, sort: str, key: str, google: list[dict], *, mode: str | None, origin_all: bool,
          start: datetime | None) -> tuple[list[tuple[dict, float | None]], int]:
    """The list in export order, each record with its rating as a share of its scale (numbers, never
    the localised text), and how many records lead it settled by what is stored.

    ``partners``: Travel's order places the partner reviews among Google's; Google's rows keep Search's
    order (a Google row Travel did not list stays at its Search place, after the settled part).  The
    other modes: Travel's order is the list, settled up to the first Google record not looked up yet
    (no row, and not a Search record before the start date); a finished list looks up at least
    ``maxReviews`` settled records, so the export's head never reaches past them.  Partner reviews get
    their day only by link (:func:`partner_day`) and leave by it (:func:`keep_partner`).  Without
    Travel rows the Google rows are the list."""
    order = store.travel_order(fid, sort, key)
    if not order:
        # partners: nothing is placed before Travel's list is read
        return [(g, _stars(g)) for g in google], 0 if mode == "partners" else len(google)
    days = card_days(store, fid)
    partners = {}
    for r in store.db.execute("SELECT * FROM partner_reviews WHERE fid = ?", (fid,)):
        rec = loads(r["data"]) or {}
        rec["publishedAtDate"] = partner_day(r, days)[0]
        for k in ("text", "textTranslated"):
            if isinstance(rec.get(k), str):
                rec[k] = strip_highlight(rec[k])  # rows of a filtered list stored before rc35 kept the markup
        if keep_partner(rec, start):
            partners[(r["origin"], r["review_id"])] = (rec, _ratio(r["rating"], r["scale"]))
    by_id = {g["reviewId"]: g for g in google}
    out: list[tuple[dict, float | None]] = []
    if mode == "partners":
        g_index = {g["reviewId"]: i for i, g in enumerate(google)}
        next_g = 0
        for row in order:
            if row["origin"] == "google":
                i = g_index.get(row["review_id"])
                if i is None or i < next_g:
                    continue
                out.extend((g, _stars(g)) for g in google[next_g:i + 1])
                next_g = i + 1
            elif origin_all and (row["origin"], row["review_id"]) in partners:
                out.append(partners[(row["origin"], row["review_id"])])
        settled = len(out)
        out.extend((g, _stars(g)) for g in google[next_g:])
        return out, settled
    listed = {r[0] for r in store.db.execute("SELECT review_id FROM reviews WHERE fid = ? AND sort = ? AND query = ?",
                                             (fid, sort, key))}
    old = set() if start is None else {r[0] for r in store.db.execute(
        "SELECT review_id FROM search_pool WHERE fid = ? AND list_time_ms < ?", (fid, int(start.timestamp() * 1000)))}
    settled = None
    for row in order:
        if row["origin"] == "google":
            rid = row["review_id"]
            if rid in by_id:
                out.append((by_id[rid], _stars(by_id[rid])))
            elif settled is None and rid not in listed and rid not in old:
                settled = len(out)  # not looked up yet: the records before it are settled
        elif origin_all and (row["origin"], row["review_id"]) in partners:
            out.append(partners[(row["origin"], row["review_id"])])
    return out, len(out) if settled is None else settled


def ordered_rows(store: Any, fid: str, sort: str, key: str, google: list[dict], *, mode: str | None,
                 origin_all: bool, start: datetime | None = None) -> list[dict]:
    """The export order of a list (:func:`_walk`): Google's rows (dates merged, the start date applied)
    and the partner records placed by Travel's order.  ``complete``: Travel's relevance order sorted by
    rating as a share of the scale (ties keep it; a record without a rating goes last)."""
    rows, _ = _walk(store, fid, sort, key, google, mode=mode, origin_all=origin_all, start=start)
    if mode == "complete" and store.travel_order(fid, sort, key):
        desc = sort == "highestRanking"
        # stable: equal ratings keep Google's relevance order
        rows = sorted(rows, key=lambda x: (x[1] is None, 0.0 if x[1] is None else (-x[1] if desc else x[1])))
    return [r for r, _ in rows]


def export_counts(store: Any, run: Any, fid: str, sort: str, key: str, *, mode: str | None,
                  start: datetime | None) -> tuple[int, int]:
    """(records the export has now, how many lead it settled).  Settled: kept by the start date (Google
    reviews by the time Google shows, partner reviews by their day), placed by the part of Travel's
    list read and looked up, without Google reviews known only from Travel whose time is still unknown
    (their Maps read may drop them).  ``maxReviews`` counts the settled records."""
    google, _ = export_rows(store, fid, sort, start, query=key, activity=True)
    rows, settled = _walk(store, fid, sort, key, google, mode=mode,
                          origin_all=run.apify.get("reviewsOrigin") != "google", start=start)
    if start is None:
        return len(rows), settled
    open_ids = {r[0] for r in store.db.execute(
        "SELECT r.review_id FROM reviews r LEFT JOIN review_dates d ON d.fid = r.fid AND d.review_id = r.review_id "
        "WHERE r.fid = ? AND r.sort = ? AND r.query = ? AND r.source = 'travel' AND (d.observed_at IS NULL "
        "OR d.published_us IS NULL)", (fid, sort, key))}
    return len(rows), sum(1 for rec, _ in rows[:settled]
                          if not (rec.get("reviewOrigin") in (None, "Google") and rec.get("reviewId") in open_ids))


def settled_count(store: Any, run: Any, fid: str, sort: str, key: str, *, mode: str | None,
                  start: datetime | None) -> int:
    """The records ``maxReviews`` counts (:func:`export_counts`)."""
    return export_counts(store, run, fid, sort, key, mode=mode, start=start)[1]


def next_target(target: int, settled: int, limit: int) -> int:
    """The Travel records to look up next when fewer than ``limit`` are settled: the shortfall scaled
    by the share kept so far, at least the shortfall and at most one page more than it."""
    short = limit - settled
    step = math.ceil(short * target / settled) if settled > 0 else target
    return target + max(short, min(step, PAGE_SIZE))
