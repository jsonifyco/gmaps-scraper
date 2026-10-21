"""Exact review dates: the Maps cache, freshness rules, single-review enrichment and the merge.

The Search list (pipeline/review_list.py) gives every review but only one ambiguous time per review
(the last edit, else the publish time) and no exact owner-reply time.  Exact dates come from Maps
and are kept per review in ``review_dates`` with their origin and the time they were observed:

* ``published_us`` -- immutable once known: reused for good;
* ``edited_us`` / ``edit_state`` ('none' | 'edited') and ``owner_us`` / ``owner_state``
  ('none' | 'present') -- mutable: reused while consistent with the latest list and younger than
  ``bro.reviewsDateTtlDays``, otherwise checked again.

Consistency with the list: Search's time must equal the last edit of an edited review, else its
publish time; a different time means the review was edited since.  A changed owner-reply text,
or a reply that appeared or went away, means the reply date must be read again (an unchanged text
does not prove an unchanged date, hence the TTL as well).  "No reply" and "a reply whose date is
unknown" differ: a record with a reply but without its date is kept as ``status='partial'`` (the
other dates stand) and read again, within the same MAX_ATTEMPTS as a failed read.

Source: ``maps_review`` (a single-review page, maps/review_page.py) -- Maps is used only for this
enrichment; the list always comes from Search.  Observations are kept in the job and, with
``bro.reviewsDateCache``, in a file shared by later jobs, so a place collected again needs Maps
reads only for new or changed reviews (a publish date never changes).  A failed read never overwrites
values observed before and never puts Search's time in their place; unknown stays unknown.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..maps.review_page import ReviewPageReader
from ..maps.reviews import parse_review
from ..store import Store, dumps, loads, now_iso
from .context import PRIORITY
from .discovery import TaskInterrupted

log = logging.getLogger(__name__)

PARSER_VERSION = "review-dates-2026.10.14b"  # partial owner dates, Search basis; Travel-only reply unknown (rc33), swept (rc34)
#: how stale an observation of a mutable date may be before it is read again
DEFAULT_TTL_DAYS = 30.0
#: failed reads of one review before it is reported unresolved (counted since the last success)
MAX_ATTEMPTS = 3
DATE_COLUMNS = ("published_us", "edited_us", "edit_state", "owner_us", "owner_state", "owner_hash", "source",
                "observed_at", "status", "attempts", "last_error", "evidence_sha", "parser_version",
                "basis_list_ms", "basis_owner_hash")


# --------------------------------------------------------------------------- conversions
def iso_us(us: int | None) -> str | None:
    if us is None:
        return None
    return datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def us_from_iso(text: str | None) -> int | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return int(round(dt.timestamp() * 1_000_000))


def dates_from_record(record: list) -> dict[str, Any]:
    """Exact dates of a Maps ``qv9Egd``-layout record (list or single-review page)."""
    from ..maps.xssi import dig
    pub, edit = dig(record, 1, 2), dig(record, 1, 3)
    review = parse_review(record)
    owner_text = review.get("responseFromOwnerText")
    return {"published_us": pub if isinstance(pub, int) else None,
            "edited_us": edit if isinstance(edit, int) and edit != pub else None,
            "edit_state": "edited" if isinstance(edit, int) and edit != pub else "none",
            "owner_us": dig(record, 3, 1) if owner_text and isinstance(dig(record, 3, 1), int) else None,
            "owner_state": "present" if owner_text else "none",
            "owner_hash": hashlib.sha1(owner_text.encode("utf-8")).hexdigest()[:16] if owner_text else None}


# --------------------------------------------------------------------------- freshness
def _observed(row: Any) -> datetime | None:
    if row is None or not row["observed_at"]:
        return None
    return datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))


def consistent_edit(row: Any, list_time_ms: int | None) -> bool:
    """Does Search's time agree with the observed edit state? (unknown list time: assume yes)"""
    if list_time_ms is None or row is None or row["published_us"] is None:
        return True
    expected = row["edited_us"] if row["edit_state"] == "edited" and row["edited_us"] else row["published_us"]
    return abs(expected // 1000 - list_time_ms) <= 1


def _col(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def ahead_of_list(row: Any, *, list_time_ms: int | None, owner_hash: str | None) -> bool:
    """Maps' observation disagrees with the list, and the list is the very snapshot that
    observation was checked against: the review changed after Search listed it (Maps is newer).
    Reading Maps again would only return the same newer state; a new list decides."""
    if row is None or row["observed_at"] is None:
        return False
    edit_ahead = (not consistent_edit(row, list_time_ms) and list_time_ms is not None
                  and _col(row, "basis_list_ms") == list_time_ms)
    owner_ahead = (not consistent_owner(row, owner_hash) and _col(row, "basis_list_ms") is not None
                   and (_col(row, "basis_owner_hash") or None) == (owner_hash or None))
    return edit_ahead or owner_ahead


#: the list does not say whether the review has an owner reply: a Google review known only from
#: Travel's list (source 'travel') -- Travel carries no reply, so its None is unknown, not "no reply"
OWNER_UNKNOWN = "?unknown"


def list_owner(rev: Any) -> str | None:
    """The owner-reply hash the list row stands for: Search's (None = no reply) or OWNER_UNKNOWN."""
    return OWNER_UNKNOWN if _col(rev, "source") == "travel" else rev["owner_hash"]


def consistent_owner(row: Any, owner_hash: str | None) -> bool:
    """Does the list's owner reply agree with the observed one?  An unknown reply agrees with any:
    nothing in the list could show a change, so a Maps observation of it stands (TTL aside)."""
    if row is None or owner_hash == OWNER_UNKNOWN:
        return True
    return (row["owner_hash"] or None) == (owner_hash or None)


def refresh_reason(row: Any, *, list_time_ms: int | None, owner_hash: str | None, origin: str | None,
                   ttl_days: float, now: datetime | None = None, max_attempts: int = MAX_ATTEMPTS) -> str | None:
    """Why this review needs a Maps read now (None = the stored dates stand)."""
    if origin and origin != "Google":
        return None  # a partner review's link leaves Google: no Maps page to read
    observed = _observed(row)
    failures = int(row["attempts"] or 0) if row is not None else 0
    if failures >= max_attempts:
        return None  # unresolved for now; reported, retried by a later run (retry-failed)
    if observed is None or row["published_us"] is None:
        return "missing"
    if not consistent_edit(row, list_time_ms) and _col(row, "basis_list_ms") != list_time_ms:
        return "edit_changed"
    if not consistent_owner(row, owner_hash) and not (
            _col(row, "basis_list_ms") is not None and (_col(row, "basis_owner_hash") or None) == (owner_hash or None)):
        return "owner_changed"
    if row["owner_state"] == "present" and row["owner_us"] is None:
        return "owner_date_missing"
    if ttl_days and (now or datetime.now(timezone.utc)) - observed > timedelta(days=ttl_days):
        return "ttl"
    return None


# --------------------------------------------------------------------------- shared cache
class DateCache:
    """Review dates shared by jobs (``bro.reviewsDateCache``): one SQLite file, one table, filled by
    this scraper's own Maps date reads."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS review_dates (fid TEXT NOT NULL, review_id TEXT NOT NULL, "
                      + ", ".join(f"{col} {'INTEGER' if col.endswith(('_us', '_ms')) or col == 'attempts' else 'TEXT'}"
                                  for col in DATE_COLUMNS)
                      + ", updated_at TEXT, PRIMARY KEY (fid, review_id))")
            have = {r[1] for r in c.execute("PRAGMA table_info(review_dates)")}
            for col in DATE_COLUMNS:
                if col not in have:  # caches written before 2026-10-08
                    c.execute(f"ALTER TABLE review_dates ADD COLUMN {col} {'INTEGER' if col.endswith('_ms') else 'TEXT'}")

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=60, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    def get(self, fid: str, review_id: str) -> sqlite3.Row | None:
        return self._conn().execute("SELECT * FROM review_dates WHERE fid = ? AND review_id = ?",
                                    (fid, review_id)).fetchone()

    def put(self, fid: str, review_id: str, fields: dict[str, Any]) -> None:
        """Store an observation unless the cache holds a newer one."""
        c = self._conn()
        with c:
            old = c.execute("SELECT observed_at FROM review_dates WHERE fid = ? AND review_id = ?",
                            (fid, review_id)).fetchone()
            if old and old[0] and fields.get("observed_at") and old[0] > fields["observed_at"]:
                return
            cols = [k for k in DATE_COLUMNS if k in fields] + ["updated_at"]
            vals = [fields[k] for k in cols[:-1]] + [now_iso()]
            c.execute(f"INSERT INTO review_dates (fid, review_id, {', '.join(cols)}) VALUES (?, ?, {', '.join('?' * len(cols))}) "
                      f"ON CONFLICT (fid, review_id) DO UPDATE SET {', '.join(f'{k} = excluded.{k}' for k in cols)}",
                      (fid, review_id, *vals))


def cache_for(ctx) -> DateCache | None:
    path = getattr(ctx.run.bro, "reviewsDateCache", None)
    if not path:
        return None
    with ctx.lock:
        cache = getattr(ctx, "_date_cache", None)
        if cache is None or str(cache.path) != str(path):
            cache = ctx._date_cache = DateCache(path)
    return cache


# --------------------------------------------------------------------------- merge + queue
def adopt_from_cache(store: Store, cache: DateCache | None, fid: str, review_id: str, db: sqlite3.Connection) -> Any:
    """The job's dates row, filled from the shared cache when the job has no observation yet."""
    row = store.review_dates_row(fid, review_id, db)
    if (row is None or not row["observed_at"]) and cache is not None:
        cached = cache.get(fid, review_id)
        if cached is not None and cached["observed_at"]:
            store.put_review_dates(db, fid, review_id, **{k: cached[k] for k in DATE_COLUMNS if k != "attempts"},
                                   attempts=0)
            row = store.review_dates_row(fid, review_id, db)
    return row


def needing(store: Store, fid: str, ids: Iterable[str], *, ttl_days: float, cache: DateCache | None = None,
            db: sqlite3.Connection | None = None) -> list[tuple[str, str]]:
    """(review id, reason) of the listed reviews that need a Maps read; adopts cached dates."""
    out = []
    conn = db or store.db
    for rid in ids:
        rev = conn.execute("SELECT list_time_ms, owner_hash, origin, source FROM reviews WHERE fid = ? AND review_id = ?",
                           (fid, rid)).fetchone()
        if rev is None:
            continue
        row = adopt_from_cache(store, cache, fid, rid, conn) if db is not None else store.review_dates_row(fid, rid)
        reason = refresh_reason(row, list_time_ms=rev["list_time_ms"], owner_hash=list_owner(rev),
                                origin=rev["origin"], ttl_days=ttl_days)
        if reason:
            out.append((rid, reason))
    return out


def enqueue(store: Store, fid: str, items: list[tuple[str, str]], *, batch: int, priority: int,
            db: sqlite3.Connection, exclude_task: int | None = None) -> int:
    """Queue Maps reads in tasks of ``batch`` reviews.  Reviews already waiting in a pending or running
    task of this place are not queued again (the end-of-list sweep would otherwise duplicate the
    per-page tasks, and every extra task claims a session)."""
    waiting: set[str] = set()
    for (payload,) in db.execute("SELECT payload FROM tasks WHERE kind = 'review_dates' AND status IN ('pending', 'running') "
                                 "AND json_extract(payload, '$.fid') = ? AND id != ?", (fid, exclude_task or -1)):
        waiting.update((loads(payload) or {}).get("ids") or [])
    items = [(rid, why) for rid, why in items if rid not in waiting]
    n = 0
    # a new read of the same reviews (a retry, a TTL refresh after an earlier success, a task that
    # failed) is a new task: duplicates are prevented by ``waiting`` above, not by the key, so the
    # key carries this place's task sequence number and never collides with a finished task
    seq = db.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'review_dates' AND json_extract(payload, '$.fid') = ?",
                     (fid,)).fetchone()[0]
    for i in range(0, len(items), batch):
        part = items[i:i + batch]
        chunk = [rid for rid, _ in part]
        key = f"{fid}|{seq}|{hashlib.sha1('|'.join(chunk).encode()).hexdigest()[:20]}"
        if store.add_task("review_dates", key, {"fid": fid, "ids": chunk, "why": sorted({w for _, w in part})},
                          priority=priority, db=db):
            n += 1
            seq += 1
    return n


def unfinished_tasks(db: sqlite3.Connection, fid: str, exclude_task: int | None = None) -> int:
    return db.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'review_dates' AND status IN ('pending', 'running') "
                      "AND json_extract(payload, '$.fid') = ? AND id != ?", (fid, exclude_task or -1)).fetchone()[0]


def reopen_waiting_lists(store: Store, fid: str, db: sqlite3.Connection) -> int:
    """List tasks paused for exact publish dates (maxReviews with reviewsStartDate, see
    :func:`limit_counts`) go back to the queue once no date read of the place is left."""
    if unfinished_tasks(db, fid):
        return 0
    return db.execute("UPDATE tasks SET status = 'pending', attempts = 0, updated_at = ? WHERE kind = 'reviews' "
                      "AND status IN ('waiting', 'done') AND json_extract(payload, '$.fid') = ? "
                      "AND json_extract(progress, '$.waitDates') = 1", (now_iso(), fid)).rowcount


def wake_orphaned_lists(store: Store) -> int:
    """Parked lists whose place has no date read left (a safety net for the end of a run: the
    queue must not look empty while a list waits for nothing)."""
    n = 0
    with store.tx() as db:
        for (fid,) in db.execute("SELECT DISTINCT json_extract(payload, '$.fid') FROM tasks WHERE kind = 'reviews' "
                                 "AND status = 'waiting'").fetchall():
            n += reopen_waiting_lists(store, fid, db)
    return n


def after_dates_task(ctx, task: Any) -> int:
    """Called by the worker once a ``review_dates`` task is finished (done or failed): the last one
    of a place wakes the place's paused list.  It runs after the task's own status is written, so
    of two tasks finishing together at least the second sees the other one finished."""
    fid = (loads(task["payload"]) or {}).get("fid")
    if not fid:
        return 0
    with ctx.store.tx() as db:
        n = reopen_waiting_lists(ctx.store, fid, db)
    if n:
        ctx.store.event("reviews", f"the review list of {fid} resumes: its exact publish dates are read",
                        data={"tasks": n})
    return n


def limit_counts(store: Store, fid: str, sort: str, query: str | None, start: datetime | None, *, enrich: bool,
                 db: sqlite3.Connection | None = None, activity: bool = False) -> tuple[int, int]:
    """(reviews the export will keep, reviews still waiting for an exact publish date).

    ``maxReviews`` counts exported reviews, and ``reviewsStartDate`` drops a review by its exact
    publish time, known only after its Maps read: an old review edited lately passes Search's
    pre-filter and is dropped later.  Reviews whose date will not come (partner reviews, reads given
    up) and, in fast mode, every review without one are exported with a null date, so they count."""
    conn = db or store.db
    if start is None or activity:
        # core-2026.10 filters by the time Google shows (the last edit, else the publish time) -- Search's
        # list time, which already dropped every older review when it was listed: nothing waits for Maps
        return conn.execute("SELECT COUNT(*) FROM reviews WHERE fid = ? AND sort = ? AND query = ?",
                            (fid, sort, query or "")).fetchone()[0], 0
    start_us = int(round(start.timestamp() * 1_000_000))
    known_new, unknown, final = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN d.observed_at IS NOT NULL AND d.published_us IS NOT NULL "
        "AND d.published_us >= ? THEN 1 ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN d.observed_at IS NULL OR d.published_us IS NULL THEN 1 ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN (d.observed_at IS NULL OR d.published_us IS NULL) AND ((r.origin IS NOT NULL "
        "AND r.origin != 'Google') OR COALESCE(d.attempts, 0) >= ?) THEN 1 ELSE 0 END), 0) "
        "FROM reviews r LEFT JOIN review_dates d ON d.fid = r.fid AND d.review_id = r.review_id "
        "WHERE r.fid = ? AND r.sort = ? AND r.query = ?", (start_us, MAX_ATTEMPTS, fid, sort, query or "")).fetchone()
    if not enrich:
        return known_new + unknown, 0
    return known_new + final, unknown - final


def refresh_section(store: Store, fid: str, *, ttl_days: float, sort: str | None = None) -> dict[str, Any]:
    """``reviewDates`` section: completeness of exact dates, apart from the list's completeness."""
    revs = store.db.execute("SELECT review_id, list_time_ms, owner_hash, origin, source FROM reviews WHERE fid = ? "
                            "AND source IN ('search', 'travel')" + (" AND sort = ?" if sort else ""),
                            (fid, sort) if sort else (fid,)).fetchall()
    dates = {d["review_id"]: d for d in store.db.execute("SELECT * FROM review_dates WHERE fid = ?", (fid,))}
    # exact: every date of the review known (publish, edit, the owner reply's when there is one);
    # refresh: exact, but a new read is due (edited since, reply changed, older than the TTL);
    # partial: publish date known, owner reply present without its date
    # ahead: Maps changed after the list was read (new edit or owner reply): the changed date is
    # left out of the export until a new list carries the change
    counts = {"listed": len(revs), "exact": 0, "refresh": 0, "partial": 0, "partner": 0, "unresolved": 0,
              "pending": 0, "stale": 0, "ahead": 0}
    unresolved_ids = []
    for r in revs:
        if r["origin"] and r["origin"] != "Google":
            counts["partner"] += 1
            continue
        d = dates.get(r["review_id"])
        has = d is not None and d["observed_at"] is not None and d["published_us"] is not None
        # why a read is due, ignoring the failure cap (the cap decides pending vs given up)
        reason = refresh_reason(d, list_time_ms=r["list_time_ms"], owner_hash=list_owner(r), origin=r["origin"],
                                ttl_days=ttl_days, max_attempts=10 ** 9)
        failures = int(d["attempts"] or 0) if d is not None else 0
        if has and ahead_of_list(d, list_time_ms=r["list_time_ms"], owner_hash=list_owner(r)):
            counts["ahead"] += 1
            if reason:
                counts["pending" if failures < MAX_ATTEMPTS else "stale"] += 1
        elif has and not (d["owner_state"] == "present" and d["owner_us"] is None):
            counts["exact"] += 1
            if reason:
                counts["refresh"] += 1
                counts["pending" if failures < MAX_ATTEMPTS else "stale"] += 1
        elif has:
            counts["partial"] += 1
            if failures < MAX_ATTEMPTS:
                counts["pending"] += 1
            elif len(unresolved_ids) < 50:
                unresolved_ids.append({"reviewId": r["review_id"], "status": d["status"], "error": d["last_error"]})
        elif failures >= MAX_ATTEMPTS:
            counts["unresolved"] += 1
            if len(unresolved_ids) < 50:
                unresolved_ids.append({"reviewId": r["review_id"], "status": d["status"], "error": d["last_error"]})
        else:
            counts["pending"] += 1
    if counts["listed"] == 0:
        status, reason = "not_requested", None
    elif counts["pending"]:
        status, reason = "running", f"{counts['pending']} reviews wait for their Maps dates"
    elif counts["unresolved"] or counts["partner"] or counts["stale"] or counts["partial"] or counts["ahead"]:
        status = "limited"
        parts = []
        if counts["ahead"]:
            parts.append(f"{counts['ahead']} reviews changed on Maps after the list was read (new edit or owner "
                         "reply): the changed date is left empty, not paired with the older listed text")
        if counts["unresolved"]:
            parts.append(f"{counts['unresolved']} reviews without exact dates after {MAX_ATTEMPTS} Maps reads")
        if counts["partial"]:
            parts.append(f"{counts['partial']} reviews whose owner reply has no date in Maps' record after "
                         f"{MAX_ATTEMPTS} reads (their other dates are exact)")
        if counts["partner"]:
            parts.append(f"{counts['partner']} partner reviews (their links leave Google: no Maps dates)")
        if counts["stale"]:
            parts.append(f"{counts['stale']} reviews with dates observed earlier and not re-confirmed")
        reason = "; ".join(parts)
    else:
        status, reason = "done", "every listed review has exact Maps dates"
    store.set_section(fid, "reviewDates", status, reason=reason, count=counts["exact"], expected=counts["listed"],
                      detail={**counts, "unresolvedSample": unresolved_ids})
    return {"status": status, **counts}


# --------------------------------------------------------------------------- enrichment task
def handle_review_dates(ctx, maps, task: Any) -> dict:
    """Read exact dates of a batch of listed reviews from their Maps single-review pages.

    Each review's outcome is committed on its own, so a task that comes back to the queue (session
    replaced after an unknown command outcome, run stopped) skips the reviews already done."""
    store = ctx.store
    p = loads(task["payload"])
    fid, ids = p["fid"], list(p["ids"])
    ttl = ctx.run.bro.reviewsDateTtlDays
    cache = cache_for(ctx)
    reader = ReviewPageReader(maps)
    reader.recycle = ctx.run.bro.reviewsPageRecycle
    done = {"ok": 0, "partial": 0, "not_found": 0, "no_answer": 0, "unreadable": 0, "server_error": 0, "skipped": 0}
    for rid in ids:
        if ctx.should_stop():
            refresh_section(store, fid, ttl_days=ttl)
            raise TaskInterrupted(f"run_{ctx.stop_reason or 'stop'}")
        rev = store.db.execute("SELECT list_time_ms, owner_hash, origin, review_url, source FROM reviews "
                               "WHERE fid = ? AND review_id = ?", (fid, rid)).fetchone()
        if rev is None:
            done["skipped"] += 1
            continue
        with store.tx() as db:
            row = adopt_from_cache(store, cache, fid, rid, db)
        reason = refresh_reason(row, list_time_ms=rev["list_time_ms"], owner_hash=list_owner(rev),
                                origin=rev["origin"], ttl_days=ttl)
        if reason is None:
            done["skipped"] += 1
            continue
        result = reader.fetch(fid, rid, rev["review_url"])
        attempts = int(row["attempts"] or 0) if row is not None else 0
        fields = None
        if result.status == "ok":
            fields = {**dates_from_record(result.record), "source": "maps_review", "observed_at": now_iso(),
                      "status": "ok", "attempts": 0, "last_error": None, "parser_version": PARSER_VERSION,
                      # the Search snapshot this observation was checked against (see ahead_of_list)
                      "basis_list_ms": rev["list_time_ms"], "basis_owner_hash": rev["owner_hash"]}
            if fields["owner_state"] == "present" and fields["owner_us"] is None:
                # a reply without its date: the other dates stand, the reply date is read again
                fields.update(status="partial", attempts=attempts + 1,
                              last_error=f"{reason}: owner reply without a date in the Maps record")
        failed = fields is None or fields["status"] != "ok"
        sha = None
        if result.raw:
            # failed reads are kept under evidence='errors' too: evidence_sha must point to a file
            sha = hashlib.sha256(result.raw.encode("utf-8")).hexdigest()
            ctx.evidence(kind="review_page", url=f"pc:{fid}:{rid}", status=result.http_status or 0, text=result.raw,
                         task_id=task["id"], note=f"{result.status}: {result.note}" if result.note else result.status,
                         failed=failed)
        with store.tx() as db:
            if fields is not None:
                fields["evidence_sha"] = sha
                store.put_review_dates(db, fid, rid, **fields)
                if cache is not None:
                    cache.put(fid, rid, fields)
            else:
                # earlier observations stay as they were; only the failure is recorded
                store.put_review_dates(db, fid, rid, status=result.status, attempts=attempts + 1,
                                       last_error=f"{reason}: {result.note}"[:300], evidence_sha=sha)
        outcome = fields["status"] if fields is not None else result.status
        done[outcome] = done.get(outcome, 0) + 1
    # reviews of this task that failed but have reads left go back to the queue at once (a later
    # task, so the run does not end with them waiting and nothing queued)
    with store.tx() as db:
        retry = needing(store, fid, ids, ttl_days=ttl, db=db)
        if retry:
            enqueue(store, fid, retry, batch=ctx.run.bro.reviewsEnrichBatch, priority=PRIORITY.get("review_dates", 42) + 6,
                    db=db, exclude_task=task["id"])
    section = refresh_section(store, fid, ttl_days=ttl)
    return {"fid": fid, **done, "section": section["status"]}


def sweep(ctx) -> int:
    """Queue Maps reads for every listed review that still needs one and waits in no task (run
    start / resume).  Reviews given up after MAX_ATTEMPTS stay out (``retry-failed`` resets them)."""
    store, bro = ctx.store, ctx.run.bro
    cache = cache_for(ctx)
    n = 0
    # Google reviews listed by Search and those known only from Travel (stubs): the same TTL and missing
    # dates apply to both (partner reviews live in partner_reviews and have no Maps page)
    fids = [r[0] for r in store.db.execute("SELECT DISTINCT fid FROM reviews WHERE source IN ('search', 'travel')")]
    for fid in fids:
        with store.tx() as db:
            ids = [r[0] for r in db.execute("SELECT review_id FROM reviews WHERE fid = ? AND source IN ('search', 'travel')",
                                            (fid,))]
            due = needing(store, fid, ids, ttl_days=bro.reviewsDateTtlDays, cache=cache, db=db)
            if due:
                n += enqueue(store, fid, due, batch=bro.reviewsEnrichBatch, priority=PRIORITY.get("review_dates", 42), db=db)
            reopen_waiting_lists(store, fid, db)
        refresh_section(store, fid, ttl_days=bro.reviewsDateTtlDays)
    return n


# --------------------------------------------------------------------------- export merge
def merged_review(data: dict, rev: Any, dates: Any, *, fast: bool = False) -> dict:
    """A stored review with its exact dates.  Search-listed reviews get dates only from Maps
    observations; anything not observed (or no longer consistent with the list) stays null.
    ``fast`` (bro.reviewsMode 'fast') adds Search's one time apart, as publishedOrEditedAtDate."""
    out = dict(data)
    if rev is None or rev["source"] not in ("search", "travel"):
        return out  # a review stored by the Maps list parser carries Maps' own dates
    pub = edited = owner = None
    if dates is not None and dates["observed_at"] and dates["published_us"] is not None:
        pub = iso_us(dates["published_us"])
        if consistent_edit(dates, rev["list_time_ms"]):
            edited = iso_us(dates["edited_us"]) if dates["edit_state"] == "edited" else None
        if dates["owner_state"] == "present" and consistent_owner(dates, list_owner(rev)):
            owner = iso_us(dates["owner_us"])
    out["publishedAtDate"] = pub
    out["lastEditedAtDate"] = edited
    out["responseFromOwnerDate"] = owner if out.get("responseFromOwnerText") else None
    if fast:
        ms = rev["list_time_ms"]
        out["publishedOrEditedAtDate"] = iso_us(ms * 1000) if ms is not None else None
    return out


def export_rows(store: Store, fid: str, sort: str, start: datetime | None = None, *,
                fast: bool = False, query: str = "", activity: bool = False) -> tuple[list[dict], dict]:
    """Reviews of the requested list for export, dates merged and the start-date filter applied by
    exact publish time.  Reviews without an exact publish time are kept and counted.  ``query`` is
    the normalised ``reviewsFilterString`` ("" = no filter): only reviews stored under it belong."""
    rows = store.db.execute(
        "SELECT r.*, d.published_us, d.edited_us, d.edit_state, d.owner_us, d.owner_state, "
        "d.owner_hash AS d_owner_hash, d.observed_at FROM reviews r LEFT JOIN review_dates d "
        "ON d.fid = r.fid AND d.review_id = r.review_id WHERE r.fid = ? AND r.sort = ? AND COALESCE(r.query, '') = ? "
        "ORDER BY r.attempt DESC, r.page, r.position", (fid, sort, query or "")).fetchall()
    out, unresolved = [], 0
    for r in rows:
        dates = _DatesView(r) if r["observed_at"] is not None else None
        review = merged_review(loads(r["data"]) or {}, r, dates, fast=fast)
        if start is not None and r["source"] in ("search", "travel"):
            pub = review.get("publishedAtDate")
            if activity:
                # the time Google shows: the last edit, else the publish time (Search's list time stands
                # in while the Maps dates are not read)
                ms = r["list_time_ms"]
                pub = review.get("lastEditedAtDate") or pub or (iso_us(ms * 1000) if ms is not None else None)
            if pub is None:
                unresolved += 1
            elif datetime.fromisoformat(pub.replace("Z", "+00:00")) < start:
                continue
        out.append(review)
    return out, {"startDateUnresolved": unresolved}


class _DatesView:
    """The review_dates columns of a joined row, under their own names."""

    def __init__(self, r: Any):
        self._r = r

    def __getitem__(self, key: str) -> Any:
        if key == "owner_hash":
            return self._r["d_owner_hash"]
        return self._r[key]
