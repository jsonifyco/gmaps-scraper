"""A place's review list from Google Search, with exact dates queued from Maps.

The one review route of the scraper (2026-10-07):

1. **List** -- Search's reviews endpoint (maps/search_reviews.py), 60 reviews per request from the
   first page; every page is stored with its cursors in one transaction, and the task's progress
   keeps the next cursor, so an interrupted list continues where it stopped, in any session (the
   cursor is a session-independent position key).
2. **Merge** -- exact dates already observed (this job, or the shared cache
   ``bro.reviewsDateCache``) are reused per (place, review id) under the freshness rules of
   pipeline/review_dates.py.
3. **Enrich** -- reviews without trustworthy dates are queued as ``review_dates`` tasks (single
   review pages of Maps).  Search does not tell which reviews were edited, so every review without
   an observed Maps date is queued, not only the edited ones.

Completeness is reported apart: section ``reviews`` for the list, ``reviewDates`` for the dates.
A failed date read keeps the review, with unknown dates and the reason.

``maxReviews`` counts the reviews the export keeps.  With ``reviewsStartDate`` that depends on
exact publish dates (Search's time is the last activity: an old review edited lately passes its
pre-filter), so in exact mode a list that has enough candidates only with reviews still waiting
for their date pauses (``waitDates``) and is woken when the place's date reads are finished
(review_dates.after_dates_task): it ends if enough reviews are kept, else it lists further.

``reviewsFilterString`` is applied here, to every listed review, before the start date and the
limit (pipeline/review_filter.py: no request field of the Search endpoint filters by keyword,
2026-10-07).  Only matching reviews are stored (under the normalised phrase in ``reviews.query``),
so only they spend ``maxReviews`` and get Maps date reads; pages without a match are read on to
the end of the list, the limit or the run's stop, and the counters of read and matching records
are saved with the cursor in the page's transaction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..maps.client import MapsClient
from ..maps.search_reviews import (PARSER_VERSION as SEARCH_PARSER, SearchRefused, SearchReviewsLister,
                                   SearchUnreadable, list_time_ms, owner_hash, parse_search_review)
from ..store import loads, now_iso
from .context import PRIORITY, RunContext
from .discovery import NeedFreshSession, TaskInterrupted
from .review_dates import cache_for, enqueue, limit_counts, needing, refresh_section, unfinished_tasks
from .review_filter import describe as describe_filter, match as match_phrase
from .review_travel import (TravelIdentityError, card_days, export_counts, hydrate, list_key, next_target, partner_day,
                            read_travel, settled_count, store_stubs, travel_mode)

log = logging.getLogger(__name__)

#: fresh sessions tried after Search refused the list (its unusual-traffic page) before giving up
REFUSED_SESSION_RETRIES = 2
#: a list that ends below this share of Google's review count is reported as limited, not done
SHORT_END_SHARE = 0.95
#: newest-order pages older than reviewsStartDate (by Search's time) that end the list (default)
DATE_CUTOFF_PAGES = 2


def _list_count(store, fid: str, sort: str, query: str | None, db=None) -> int:
    return (db or store.db).execute("SELECT COUNT(*) FROM reviews WHERE fid = ? AND sort = ? AND query = ?",
                                    (fid, sort, query or "")).fetchone()[0]


def _within_limit(store, fid: str, sort: str, query: str | None, rows: list[dict], limit: int) -> list[dict]:
    """The page's rows that fit into maxReviews: a review already stored (or repeated within the
    page) is an update and takes no place; only new distinct ids spend the remaining allowance."""
    stored = {r[0] for r in store.db.execute("SELECT review_id FROM reviews WHERE fid = ? AND sort = ? AND query = ?",
                                             (fid, sort, query or ""))}
    room, seen, out = limit - len(stored), set(), []
    for row in rows:
        rid = row["review"]["reviewId"]
        if rid in stored or rid in seen:
            if rid not in seen:
                out.append(row)
            seen.add(rid)
            continue
        if room <= 0:
            continue
        room -= 1
        seen.add(rid)
        out.append(row)
    return out


def _pause_for_dates(ctx, task, progress: dict, fid: str, sort: str, query: str, page_no: int, queued: int,
                     limit: int, waiting: int, expected, cache) -> bool:
    """Enough candidates only if the reviews still without a publish date are recent: park the
    list (task status ``waiting``) until the place's date reads are finished; the last one wakes it
    (review_dates.after_dates_task).  The parking and the check for unfinished date tasks happen in
    one transaction, and the date side wakes only after its own task is finished, so whichever
    worker commits last sees the other's state: a wake-up cannot fall between them.  With no date
    read left to wait for, the list does not park and reads on (False)."""
    store, run = ctx.store, ctx.run
    with store.tx() as db:
        ids = [r[0] for r in db.execute("SELECT review_id FROM reviews WHERE fid = ? AND sort = ? AND source = 'search'",
                                        (fid, sort))]
        queued += enqueue(store, fid, needing(store, fid, ids, ttl_days=run.bro.reviewsDateTtlDays, cache=cache, db=db),
                          batch=run.bro.reviewsEnrichBatch, priority=PRIORITY.get("review_dates", 45), db=db)
        if not unfinished_tasks(db, fid):
            return False
        try:
            lease = task["lease_gen"]
        except (IndexError, KeyError):
            lease = None
        progress.update(waitDates=True, queued=queued, parkedLease=lease)
        store.save_progress(task["id"], progress, db)
        db.execute("UPDATE tasks SET status = 'waiting', updated_at = ? WHERE id = ?", (now_iso(), task["id"]))
        total = _list_count(store, fid, sort, query, db)
        store.set_section(fid, "reviews", "running", count=total, expected=expected, db=db,
                          reason=(f"maxReviews={limit} with reviewsStartDate: {waiting} listed reviews wait for their "
                                  "exact publish date before the list ends or goes on"),
                          detail={"source": "search", "pages": page_no, "collected": total, "order": sort,
                                  "listEnd": "waiting_for_dates", "waitingDates": waiting})
    return True


def handle_reviews(ctx: RunContext, maps: MapsClient, task: Any) -> dict:
    run, store = ctx.run, ctx.store
    p = loads(task["payload"])
    fid, sort, limit = p["fid"], p["sort"], p.get("limit")
    # the list's key: "" (no filter), the normalised phrase (local filter) or "google:<phrase>"
    key = list_key(run, p.get("query"))
    mode = travel_mode(run, store, fid, sort, key)
    if mode in ("filter", "rating", "complete", "relevant"):
        return _handle_travel_list(ctx, maps, task, p, mode=mode, key=key)
    flt = describe_filter(p.get("query")) if key and not key.startswith("google:") else None
    # reviews of a filtered list are stored under the normalised phrase ("" = no filter)
    query = flt["normalized"] if flt else ""
    expected = p.get("expected")
    # a filtered list is not measured against the place's review count (count = matching reviews)
    sec_expected = None if flt else expected
    progress = loads(task["progress"]) or {}
    scanned, matched = int(progress.get("scanned", 0)), int(progress.get("matched", 0))
    in_translation = int(progress.get("matchedTranslation", 0))

    refused = int(progress.get("refused", 0))
    if refused > REFUSED_SESSION_RETRIES or ctx.gate_budget_exhausted:
        total = _list_count(store, fid, sort, query)
        reason = (f"Search refused the review list to {refused} consecutive sessions "
                  f"({ctx.gated_sessions} refused/gated sessions in the run); {total} reviews were listed before")
        store.set_section(fid, "reviews", "limited", reason=reason, count=total, expected=sec_expected)
        refresh_section(store, fid, ttl_days=run.bro.reviewsDateTtlDays, sort=sort)
        return {"fid": fid, "status": "limited", "reason": reason}

    start = run.reviews_start()
    start_ms = int(start.timestamp() * 1000) if start is not None else None
    google_only = run.apify.get("reviewsOrigin") == "google"
    personal = bool(run.apify.get("scrapeReviewsPersonalData", True))
    enrich = run.bro.reviewsMode == "exact"
    ttl = run.bro.reviewsDateTtlDays
    cache = cache_for(ctx)
    cutoff_pages = run.bro.reviewsDateCutoffPages
    page_no = int(progress.get("page", 0))
    cursor = progress.get("cursor")
    old_pages = int(progress.get("oldPages", 0))
    queued = int(progress.get("queued", 0))
    store.set_section(fid, "reviews", "running", expected=sec_expected)
    lister = SearchReviewsLister(maps, fid, sort=sort)
    stop_reason = None
    ended = bool(progress.get("ended"))
    waiting = 0
    if progress.get("waitDates"):
        progress = {**progress, "waitDates": False}
        with store.tx() as db:
            store.save_progress(task["id"], progress, db)

    while not ended:
        if limit is not None:
            kept, waiting = limit_counts(store, fid, sort, query, start, enrich=enrich, activity=run.bro.core_contract)
            if kept >= limit:
                stop_reason = "limit"
                break
            if kept + waiting >= limit and _pause_for_dates(ctx, task, progress, fid, sort, query, page_no, queued,
                                                            limit, waiting, sec_expected, cache):
                stop_reason = "wait_dates"
                break
        try:
            page = lister.page(cursor)
        except SearchRefused as exc:
            with store.tx() as db:
                store.save_progress(task["id"], {**progress, "refused": refused + 1}, db)
            store.event("gated", f"reviews of {fid}: {exc}", level="warning", data={"attempt": refused + 1})
            raise NeedFreshSession(f"Search refused the review list: {exc}") from exc
        except SearchUnreadable as exc:
            ctx.evidence(kind="reviews_search", url=f"GetLocalBoqProxy:{fid}:{sort}:{page_no + 1}", status=200,
                         text=exc.raw or str(exc), task_id=task["id"], note=str(exc)[:300], failed=True)
            raise RuntimeError(f"unreadable Search review answer: {exc}") from exc
        page_no += 1
        ctx.evidence(kind="reviews_search", url=f"GetLocalBoqProxy:{fid}:{sort}:{page_no}", status=200,
                     text=page.raw, task_id=task["id"])
        rows, times = [], []
        for rec in page.records:
            review = parse_search_review(rec, hl=ctx.hl, personal_data=personal)
            ms = list_time_ms(rec)
            times.append(ms)
            scanned += 1
            if flt:
                # the phrase first, then the start date and the limit (a review must pass all)
                where = match_phrase(review, query)
                if where is None:
                    continue
                matched += 1
                in_translation += where == "translation"
            origin = review.get("reviewOrigin")
            if google_only and (origin or "Google") != "Google":
                continue
            if start_ms is not None and ms is not None and ms < start_ms:
                # Search's time is the last edit, else the publish time: a review whose latest
                # time is before the cutoff was certainly published before it
                continue
            full_url = parse_search_review(rec, hl=ctx.hl)["reviewUrl"]  # kept for enrichment
            rows.append({"review": review, "listTimeMs": ms, "ownerHash": owner_hash(review.get("responseFromOwnerText")),
                         "reviewUrl": full_url, "origin": origin})
        if start_ms is not None:
            page_old = bool(times) and all(t is not None and t < start_ms for t in times)
            old_pages = old_pages + 1 if page_old else 0
        elif limit is not None:
            # without a start date the export is the first maxReviews of the list: reviews past it
            # are not stored, so no Maps date read is spent on them (a page holds 60)
            rows = _within_limit(store, fid, sort, query, rows, limit)
        with store.tx() as db:
            store.add_search_page(db, fid, sort, query or "", page_no, cursor, page.next_cursor, rows, SEARCH_PARSER)
            total = _list_count(store, fid, sort, query, db)
            store.set_section(fid, "reviews", "running", count=total, cursor=page.next_cursor, db=db,
                              detail=({"filter": {**flt, "scanned": scanned, "matched": matched,
                                                  "matchedInTranslation": in_translation, "kept": total}}
                                      if flt else None))
            if rows:
                # cached exact dates are adopted in both modes; only "exact" queues Maps reads
                due = needing(store, fid, [r["review"]["reviewId"] for r in rows], ttl_days=ttl, cache=cache, db=db)
                if enrich:
                    queued += enqueue(store, fid, due, batch=run.bro.reviewsEnrichBatch,
                                      priority=PRIORITY.get("review_dates", 45), db=db)
            progress = {**progress, "page": page_no, "cursor": page.next_cursor, "oldPages": old_pages,
                        "queued": queued, "refused": 0, "ended": page.next_cursor is None,
                        "scanned": scanned, "matched": matched, "matchedTranslation": in_translation}
            store.save_progress(task["id"], progress, db)
        refused = 0
        if page.next_cursor is None:
            ended = True
            break
        cursor = page.next_cursor
        if cutoff_pages and old_pages >= cutoff_pages and sort == "newest":
            stop_reason = "date_cutoff"
            break
        if ctx.should_stop():
            stop_reason = f"run_{ctx.stop_reason or 'stop'}"
            break

    partner_detail, partner_failed = {}, None
    if mode == "partners" and stop_reason in (None, "limit", "date_cutoff"):
        # the partner reviews Google shows and their places among Google's: Travel's list of this order,
        # as deep as the Google reviews listed (to its end when Search's list was read to the end)
        listed = {r[0] for r in store.db.execute("SELECT review_id FROM reviews WHERE fid = ? AND sort = ? AND query = ?",
                                                 (fid, sort, query))}
        try:
            # the limit counts the merged export: Travel is read until that many records of it are settled
            progress = read_travel(ctx, maps, task, progress, fid=fid, sort=sort, key=query, mode=mode,
                                   want=limit, cover=None if stop_reason is None else listed,
                                   settled=lambda: settled_count(store, run, fid, sort, query, mode=mode, start=start))
        except TaskInterrupted as exc:
            total = _list_count(store, fid, sort, query)
            store.set_section(fid, "reviews", "limited", reason=str(exc)[4:], count=total, expected=sec_expected)
            raise
        except (RuntimeError, TravelIdentityError) as exc:
            # a partner source that stays unreadable never costs the Google reviews: on the last attempt
            # the list keeps them and ends limited with the reason (session errors go to the worker)
            if int(task["attempts"] or 0) < run.bro.maxTaskAttempts:
                raise
            partner_failed = str(exc)[:300]
        partner_detail = _partner_detail(store, fid, sort, query, progress)
        if partner_failed:
            partner_detail["travel"]["error"] = partner_failed
    total = _list_count(store, fid, sort, query)
    filter_detail = ({"filter": {**flt, "scanned": scanned, "matched": matched, "matchedInTranslation": in_translation,
                                 "kept": total}} if flt else {})
    if stop_reason and stop_reason.startswith("run_"):
        store.set_section(fid, "reviews", "limited", reason=stop_reason[4:], count=total, expected=sec_expected,
                          detail=filter_detail or None)
        raise TaskInterrupted(stop_reason)
    if stop_reason == "wait_dates":
        refresh_section(store, fid, ttl_days=ttl, sort=sort)
        return {"fid": fid, "status": "waiting", "reason": f"maxReviews={limit}: waiting for {waiting} publish dates",
                "pages": page_no, "collected": total}
    if stop_reason == "limit":
        status, reason = "limited", f"maxReviews={limit}"
    elif stop_reason == "date_cutoff":
        # a heuristic end: it relies on Google's newest order following the last activity time
        status, reason = "done", (f"reviewsStartDate reached: {cutoff_pages} consecutive pages listed entirely "
                                  "before the date by their last activity time; the rest of the list is assumed "
                                  "older (not verified: bro.reviewsDateCutoffPages=0 lists to the end)")
    else:
        status, reason = "done", "source_end"
        # a filtered list is complete when the whole list was read: compare the records read
        read = scanned if flt else total
        if (isinstance(expected, int) and start is None and not google_only and read < expected * SHORT_END_SHARE):
            status, reason = "limited", (f"Search's {sort} list ended after {read} of {expected} reviews were read"
                                         if flt else f"Search's {sort} list ended at {total} of {expected} reviews")
    if partner_failed:
        status, reason = "limited", f"{reason}; partner reviews not read (Google reviews kept): {partner_failed}"
    detail = {"source": "search", "mode": mode, "pages": page_no, "collected": total, "expected": expected, "order": sort,
              "listEnd": "confirmed" if ended else ("date_cutoff_assumed" if stop_reason == "date_cutoff" else stop_reason),
              "dateTasks": queued,
              "gap": (expected - total) if isinstance(expected, int) and not flt else None, **filter_detail,
              **partner_detail}
    store.set_section(fid, "reviews", status, reason=reason, count=total, expected=sec_expected, detail=detail)
    if enrich:
        with store.tx() as db:
            # a final sweep: reviews listed in earlier attempts or whose cached dates went stale
            ids = [r[0] for r in db.execute("SELECT review_id FROM reviews WHERE fid = ? AND sort = ? AND source = 'search'",
                                            (fid, sort))]
            due = needing(store, fid, ids, ttl_days=ttl, cache=cache, db=db)
            queued += enqueue(store, fid, due, batch=run.bro.reviewsEnrichBatch,
                              priority=PRIORITY.get("review_dates", 45), db=db)
        dates = refresh_section(store, fid, ttl_days=ttl, sort=sort)
    else:
        dates = _fast_dates_section(store, fid, sort, ttl)
    return {"fid": fid, "status": status, "reason": reason, **detail, "dates": dates.get("status")}


def _fast_dates_section(store, fid: str, sort: str, ttl: float) -> dict:
    """``reviewDates`` in fast mode: no Maps reads, only cached exact dates count."""
    dates = refresh_section(store, fid, ttl_days=ttl, sort=sort)
    exact, listed = dates["exact"], dates["listed"]
    # done only when every listed review has complete exact dates that need no new read:
    # dates past the TTL or no longer consistent with the list are history, not current
    current = exact - dates["refresh"]
    d_status = "done" if listed and current == listed else "limited"
    parts = [f"{current} of {listed} reviews have current exact dates from the cache"]
    if dates["refresh"]:
        parts.append(f"{dates['refresh']} more have cached dates due for a new read (observed over "
                     f"{ttl:g} days ago: exported as observed; or changed in the list since: the "
                     "changed edit/owner-reply date is left empty)")
    if dates["partial"]:
        parts.append(f"{dates['partial']} lack the owner-reply date")
    if dates["partner"]:
        parts.append(f"{dates['partner']} are partner reviews (no Maps dates)")
    d_reason = "bro.reviewsMode 'fast': no Maps date reads; " + "; ".join(parts)
    if exact + dates["partial"] < listed:
        d_reason += "; the others carry only Search's last-activity time (publishedOrEditedAtDate)"
    sec = store.section(fid, "reviewDates")
    store.set_section(fid, "reviewDates", d_status, reason=d_reason, count=exact, expected=listed,
                      detail={**(loads(sec["detail"]) if sec and sec["detail"] else {}), "mode": "fast"})
    return {**dates, "status": d_status}


def _partner_detail(store, fid: str, sort: str, key: str, progress: dict) -> dict:
    """What Travel added to a list: partner reviews by source, how many carry Google's day date, and
    whether Travel's list was read to its end."""
    rows = store.db.execute("SELECT p.* FROM travel_order o JOIN partner_reviews p ON p.fid = o.fid AND "
                            "p.origin = o.origin AND p.review_id = o.review_id WHERE o.fid = ? AND o.sort = ? AND o.query = ?",
                            (fid, sort, key)).fetchall()
    days = card_days(store, fid)
    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r["origin"]] = by_source.get(r["origin"], 0) + 1
    listed = store.db.execute("SELECT COUNT(*) FROM travel_order WHERE fid = ? AND sort = ? AND query = ?",
                              (fid, sort, key)).fetchone()[0]
    return {"travel": {"records": listed, "pages": int(progress.get("tPage", 0)), "ended": bool(progress.get("tEnded")),
                       "partners": len(rows), "partnersBySource": by_source,
                       "partnersWithDayDate": sum(1 for r in rows if partner_day(r, days)[0]),
                       "partnerDates": "Google's relative time; the day only where the card showed the same review "
                                       "(by its link)"}}


def _handle_travel_list(ctx: RunContext, maps: MapsClient, task: Any, p: dict, *, mode: str, key: str) -> dict:
    """A list whose selection and order come from Google's Travel list (review_travel.py): Google's
    review search (``filter``), its rating selection (``rating``), the relevance list of a place with
    partner sources (``relevant``) or its complete relevance list sorted by rating at export
    (``complete``); Google records from Search, dates from Maps.

    ``maxReviews`` counts the records the export keeps (review_travel.settled_count): with
    ``reviewsStartDate`` older records do not count, so the list is read and looked up further while
    fewer are settled.  How many Travel records are looked up is kept in the progress (``tWant``), so
    a stop or a lost session continues there.  ``complete`` is called complete only when Travel's
    list was read to its end and Google's part of it reaches the place's count."""
    run, store = ctx.run, ctx.store
    fid, sort, limit, expected = p["fid"], p["sort"], p.get("limit"), p.get("expected")
    progress = loads(task["progress"]) or {}
    sec_expected = None if mode == "filter" else expected
    enrich = run.bro.reviewsMode == "exact"
    ttl = run.bro.reviewsDateTtlDays
    cache = cache_for(ctx)
    start = run.reviews_start()
    start_ms = int(start.timestamp() * 1000) if start is not None else None
    want = None if (mode == "complete" or limit is None) else limit
    target = None if want is None else max(want, int(progress.get("tWant") or 0))
    settled = 0
    stubs = 0
    store.set_section(fid, "reviews", "running", expected=sec_expected)
    try:
        while True:
            progress = read_travel(ctx, maps, task, progress, fid=fid, sort=sort, key=key, mode=mode, want=target)
            order = store.travel_order(fid, sort, key)
            head = order if target is None else order[:target]
            needed = [r["review_id"] for r in head if r["origin"] == "google"]
            progress = hydrate(ctx, maps, task, progress, fid=fid, sort=sort, key=key, mode=mode, needed=needed,
                               start_ms=start_ms)
            stubs = store_stubs(store, fid, sort, key, needed, hl=ctx.hl)
            if target is None:
                break
            settled = settled_count(store, run, fid, sort, key, mode=mode, start=start)
            if settled >= limit or (progress.get("tEnded") and target >= len(order)):
                break
            # records the export drops (before the start date) took part of the limit: look up further
            target = next_target(target, settled, limit)
            progress["tWant"] = target
            with store.tx() as db:
                store.save_progress(task["id"], progress, db)
    except TaskInterrupted as exc:
        total = export_counts(store, run, fid, sort, key, mode=mode, start=start)[0]
        store.set_section(fid, "reviews", "limited", reason=str(exc)[4:],
                          count=total if limit is None else min(limit, total), expected=sec_expected)
        raise
    except TravelIdentityError:
        raise
    order = store.travel_order(fid, sort, key)
    exported, settled = export_counts(store, run, fid, sort, key, mode=mode, start=start)
    total = exported if limit is None else min(limit, exported)
    google_n = sum(1 for r in order if r["origin"] == "google")
    ended = bool(progress.get("tEnded"))
    reason, status = "source_end", "done"
    completeness = None
    if want is not None and settled >= limit:
        status, reason = "limited", f"maxReviews={limit}"
    elif mode == "complete":
        # proof of a complete rating order: Travel's list read to its end (every partner review Google
        # shows; partner sites hold more) and Google's part reaching the place's count -- the tolerance
        # of an ordinary list end (SHORT_END_SHARE) is no proof
        complete = ended and isinstance(expected, int) and google_n >= expected
        completeness = {"complete": complete, "googleListed": google_n, "googleCount": expected, "travelEnded": ended,
                        "partners": "every partner review Google's list shows when its end was read"}
        if not complete:
            status = "limited"
            if not ended:
                reason = "Travel's relevance list was not read to its end: the rating order is not complete"
            elif not isinstance(expected, int):
                reason = "the place's review count is unknown: the rating order cannot be shown complete"
            else:
                reason = (f"the relevance list ended after {google_n} of {expected} Google reviews: the rating order "
                          "is not complete")
            completeness["reason"] = reason
    elif mode in ("rating", "relevant") and isinstance(expected, int) and google_n < expected * SHORT_END_SHARE:
        # Google's rating selection (or a relevance list) that stops before the count is an end of
        # the selection, never proof of completeness
        status = "limited"
        reason = f"Google's {sort} list ended after {google_n} of {expected} Google reviews"
    detail = {"source": "travel+search", "mode": mode, "order": sort, "collected": total, "expected": expected,
              "listEnd": "confirmed" if ended else "limit", "travelOnlyGoogle": stubs,
              **_partner_detail(store, fid, sort, key, progress)}
    if start is not None and want is not None:
        detail["startDate"] = {"settled": settled, "lookedUp": target, "basis": "the time Google shows (Search's "
                               "list time); partner reviews by their day, kept without one"}
    if mode == "filter":
        detail["filter"] = {"method": "google", "phrase": p.get("query"), "key": key, "matched": len(order),
                            "kept": total}
    if mode == "complete":
        detail["ratingOrder"] = "sorted here by rating; equal ratings keep Google's relevance order"
        detail["completeness"] = completeness
    store.set_section(fid, "reviews", status, reason=reason, count=total, expected=sec_expected, detail=detail)
    if enrich:
        with store.tx() as db:
            ids = [r[0] for r in db.execute("SELECT review_id FROM reviews WHERE fid = ? AND sort = ? AND query = ? "
                                            "AND source IN ('search', 'travel')", (fid, sort, key))]
            due = needing(store, fid, ids, ttl_days=ttl, cache=cache, db=db)
            enqueue(store, fid, due, batch=run.bro.reviewsEnrichBatch, priority=PRIORITY.get("review_dates", 45), db=db)
    dates = refresh_section(store, fid, ttl_days=ttl, sort=sort) if enrich else _fast_dates_section(store, fid, sort, ttl)
    return {"fid": fid, "status": status, "reason": reason, **detail, "dates": dates.get("status")}
