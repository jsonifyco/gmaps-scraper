"""SQLite state of a job (WAL).  Everything a run learns is committed here;
exports are produced from this file only, so re-exporting never re-collects.

Section statuses (``sections.status``) keep apart what the plan requires:

* ``not_requested`` -- the input did not ask for the section;
* ``pending`` / ``running`` -- queued or in progress;
* ``done`` -- the source reported the end of the list (or the section is complete);
* ``limited`` -- stopped by a limit, the run time or a block (``reason`` says which);
* ``failed`` -- extraction failed after retries (``reason`` holds the error);
* ``absent`` -- the source has no such section for this place;
* ``empty`` -- the source's section was read and lists nothing (an order widget without provider);
* ``unsupported`` -- the scraper has no source for the section (used for Q&A from 2026-10-09 until
  the local Q&A app was found, 2026-10-13; kept for jobs of that time).

A page of results and its continuation (offset or cursor) are written in one
transaction, so a crash can lose at most the page in flight.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

#: 2 (core-parity, 2026-10-14): partner_reviews, travel_order, travel_pages -- added to older jobs on
#: open (CREATE IF NOT EXISTS); no existing table or row changes
#: 3 (rc32, 2026-10-14): search_pool -- every Search record read while a Travel list's Google ids are
#: looked up, so a list read on later finds them without reading Search again
SCHEMA_VERSION = 3
#: the tables each schema version added (the migration history names them)
ADDED_TABLES = {2: ["partner_reviews", "travel_order", "travel_pages"], 3: ["search_pool"]}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS job (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  created_at TEXT, input_json TEXT, normalized_json TEXT, status TEXT,
  stop_requested INTEGER DEFAULT 0, stop_reason TEXT, started_at TEXT, finished_at TEXT,
  runs INTEGER DEFAULT 0, versions TEXT
);
CREATE TABLE IF NOT EXISTS territories (
  id INTEGER PRIMARY KEY, label TEXT, source TEXT, geojson TEXT, area_km2 REAL, provenance TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT, status TEXT NOT NULL DEFAULT 'pending',
  priority INTEGER NOT NULL DEFAULT 50, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, lease_owner TEXT,
  lease_until REAL, progress TEXT, result TEXT, created_at TEXT, updated_at TEXT, parent_id INTEGER,
  lease_gen INTEGER NOT NULL DEFAULT 0,
  UNIQUE (kind, key)
);
CREATE INDEX IF NOT EXISTS tasks_claim ON tasks (status, priority, id);
CREATE TABLE IF NOT EXISTS places (
  fid TEXT PRIMARY KEY, place_id TEXT, cid TEXT, kgmid TEXT, title TEXT, lat REAL, lng REAL,
  search_data TEXT, details TEXT, details_at TEXT, language TEXT, parser_version TEXT,
  first_seen_at TEXT, updated_at TEXT, parse_errors TEXT
);
CREATE TABLE IF NOT EXISTS place_aliases (alias TEXT PRIMARY KEY, kind TEXT, fid TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS discoveries (
  id INTEGER PRIMARY KEY, fid TEXT NOT NULL, source TEXT NOT NULL, search_term TEXT, task_id INTEGER,
  cell_key TEXT, cell_pass TEXT, zoom REAL, page INTEGER, position INTEGER, rank INTEGER, is_ad INTEGER DEFAULT 0,
  search_page_url TEXT, input_place_id TEXT, input_start_url TEXT, parent_fid TEXT, created_at TEXT,
  UNIQUE (fid, task_id, page, position)
);
CREATE INDEX IF NOT EXISTS discoveries_fid ON discoveries (fid);
CREATE TABLE IF NOT EXISTS results (
  fid TEXT NOT NULL, search_key TEXT NOT NULL, search_term TEXT, rank INTEGER, accepted INTEGER,
  filter_reason TEXT, inside_area INTEGER, input_place_id TEXT, input_start_url TEXT, search_page_url TEXT,
  parent_fid TEXT, first_discovery_id INTEGER, created_at TEXT,
  PRIMARY KEY (fid, search_key)
);
CREATE TABLE IF NOT EXISTS sections (
  fid TEXT NOT NULL, section TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, count INTEGER DEFAULT 0,
  expected INTEGER, cursor TEXT, detail TEXT, updated_at TEXT,
  PRIMARY KEY (fid, section)
);
CREATE TABLE IF NOT EXISTS reviews (
  fid TEXT NOT NULL, review_id TEXT NOT NULL, data TEXT, published_at TEXT, stars INTEGER, sort TEXT,
  query TEXT, page INTEGER, position INTEGER, fetched_at TEXT, parser_version TEXT, attempt INTEGER DEFAULT 0,
  PRIMARY KEY (fid, review_id)
);
CREATE TABLE IF NOT EXISTS review_pages (
  fid TEXT NOT NULL, sort TEXT NOT NULL, query TEXT NOT NULL DEFAULT '', page INTEGER NOT NULL,
  request_cursor TEXT, next_cursor TEXT, ids TEXT, new_ids INTEGER, fetched_at TEXT,
  PRIMARY KEY (fid, sort, query, page)
);
CREATE TABLE IF NOT EXISTS review_dates (
  fid TEXT NOT NULL, review_id TEXT NOT NULL, published_us INTEGER, edited_us INTEGER, edit_state TEXT,
  owner_us INTEGER, owner_state TEXT, owner_hash TEXT, source TEXT, observed_at TEXT, status TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, evidence_sha TEXT, parser_version TEXT, updated_at TEXT,
  basis_list_ms INTEGER, basis_owner_hash TEXT,
  PRIMARY KEY (fid, review_id)
);
CREATE TABLE IF NOT EXISTS images (
  fid TEXT NOT NULL, photo_id TEXT NOT NULL, position INTEGER, data TEXT, fetched_at TEXT,
  PRIMARY KEY (fid, photo_id)
);
CREATE TABLE IF NOT EXISTS domains (
  domain TEXT PRIMARY KEY, status TEXT, pages INTEGER DEFAULT 0, data TEXT, error TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS contacts (
  fid TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL, domain TEXT, source_url TEXT, context TEXT,
  ownership TEXT, created_at TEXT,
  PRIMARY KEY (fid, kind, value)
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY, worker TEXT, options TEXT, created_at TEXT, closed_at TEXT, status TEXT,
  billing TEXT, total_billed REAL, commands INTEGER, busy_seconds REAL, proxy_usage TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY, task_id INTEGER, kind TEXT, url TEXT, status INTEGER, sha256 TEXT, path TEXT,
  size INTEGER, note TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts TEXT, level TEXT, kind TEXT, message TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS partner_reviews (
  fid TEXT NOT NULL, origin TEXT NOT NULL, review_id TEXT NOT NULL, source_id INTEGER, data TEXT,
  rating REAL, scale REAL, relative_time TEXT, day_date TEXT, day_source TEXT, partner_url TEXT,
  sort TEXT, query TEXT, fetched_at TEXT, parser_version TEXT,
  PRIMARY KEY (fid, origin, review_id)
);
CREATE TABLE IF NOT EXISTS travel_order (
  fid TEXT NOT NULL, sort TEXT NOT NULL, query TEXT NOT NULL DEFAULT '', position INTEGER NOT NULL,
  origin TEXT NOT NULL, review_id TEXT NOT NULL, page INTEGER, fetched_at TEXT, data TEXT,
  PRIMARY KEY (fid, sort, query, position),
  UNIQUE (fid, sort, query, origin, review_id)
);
CREATE TABLE IF NOT EXISTS travel_pages (
  fid TEXT NOT NULL, sort TEXT NOT NULL, query TEXT NOT NULL DEFAULT '', page INTEGER NOT NULL,
  request_cursor TEXT, next_cursor TEXT, records INTEGER, new_ids INTEGER, repeated INTEGER, fetched_at TEXT,
  PRIMARY KEY (fid, sort, query, page)
);
CREATE TABLE IF NOT EXISTS search_pool (
  fid TEXT NOT NULL, review_id TEXT NOT NULL, list_order TEXT, page INTEGER, list_time_ms INTEGER, data TEXT,
  fetched_at TEXT, parser_version TEXT,
  PRIMARY KEY (fid, review_id)
);
CREATE TABLE IF NOT EXISTS extras (
  fid TEXT NOT NULL, kind TEXT NOT NULL, data TEXT, source_url TEXT, observed_at TEXT, parser_version TEXT,
  evidence_sha TEXT,
  PRIMARY KEY (fid, kind)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def dumps(obj: Any) -> str | None:
    return None if obj is None else json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


#: session states bro reports for a session that no longer runs (or bills)
TERMINAL_SESSION_STATES = ("stopped", "terminated", "failed", "cancelled")


class Store:
    """Thread-safe facade: one SQLite connection per thread, WAL journal."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.db.executescript(SCHEMA)  # executescript manages its own transaction
        with self.tx() as db:
            cols = {r[1] for r in db.execute("PRAGMA table_info(reviews)")}
            if "attempt" not in cols:  # jobs created before 2026-10-02
                db.execute("ALTER TABLE reviews ADD COLUMN attempt INTEGER DEFAULT 0")
            # jobs created before 2026-10-07 (Search list + Maps dates)
            for col, decl in (("source", "TEXT"), ("list_time_ms", "INTEGER"), ("owner_hash", "TEXT"),
                              ("review_url", "TEXT"), ("origin", "TEXT")):
                if col not in cols:
                    db.execute(f"ALTER TABLE reviews ADD COLUMN {col} {decl}")
            # jobs created before 2026-10-08 (the claim generation of a task)
            if "lease_gen" not in {r[1] for r in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN lease_gen INTEGER NOT NULL DEFAULT 0")
            # jobs created before 2026-10-08 (the Search snapshot a Maps read was checked against)
            dcols = {r[1] for r in db.execute("PRAGMA table_info(review_dates)")}
            for col, decl in (("basis_list_ms", "INTEGER"), ("basis_owner_hash", "TEXT")):
                if col not in dcols:
                    db.execute(f"ALTER TABLE review_dates ADD COLUMN {col} {decl}")
            db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            old = int(db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] or 1)
            if old < SCHEMA_VERSION:
                # the new tables exist now (SCHEMA above); record the step, keep the history
                hist = loads(db.execute("SELECT value FROM meta WHERE key = 'migrations'").fetchone()[0]
                             if db.execute("SELECT 1 FROM meta WHERE key = 'migrations'").fetchone() else None) or []
                hist.append({"from": old, "to": SCHEMA_VERSION, "at": now_iso(),
                             "added": [t for v in range(old + 1, SCHEMA_VERSION + 1) for t in ADDED_TABLES.get(v, [])]})
                db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('migrations', ?)", (dumps(hist),))
                db.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))

    # ------------------------------------------------------------ plumbing
    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=60, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("PRAGMA foreign_keys=OFF")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        else:
            db.execute("COMMIT")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------ job
    def job(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM job WHERE id = 1").fetchone()

    def set_concurrency(self, n: int) -> None:
        """Change ``bro.concurrency`` of the stored input (resume with more or fewer sessions)."""
        with self.tx() as db:
            raw = loads(db.execute("SELECT input_json FROM job WHERE id = 1").fetchone()[0]) or {}
            raw.setdefault("bro", {})["concurrency"] = int(n)
            db.execute("UPDATE job SET input_json = ? WHERE id = 1", (dumps(raw),))

    def init_job(self, input_json: dict, normalized: dict, versions: dict) -> None:
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO job (id, created_at, input_json, normalized_json, status, versions) "
                       "VALUES (1, ?, ?, ?, 'created', ?)", (now_iso(), dumps(input_json), dumps(normalized), dumps(versions)))

    def set_job(self, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as db:
            db.execute(f"UPDATE job SET {cols} WHERE id = 1", tuple(fields.values()))

    def request_stop(self, reason: str) -> None:
        self.set_job(stop_requested=1, stop_reason=reason)

    def stop_requested(self) -> bool:
        row = self.db.execute("SELECT stop_requested FROM job WHERE id = 1").fetchone()
        return bool(row and row[0])

    def event(self, kind: str, message: str, *, level: str = "info", data: Any = None) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO events (ts, level, kind, message, data) VALUES (?, ?, ?, ?, ?)",
                       (now_iso(), level, kind, message, dumps(data)))

    # ---------------------------------------------------------------- tasks
    def add_task(self, kind: str, key: str, payload: dict, *, priority: int = 50, parent_id: int | None = None,
                 db: sqlite3.Connection | None = None) -> int | None:
        """Insert a task unless the same (kind, key) exists; return its id when new."""
        def run(c: sqlite3.Connection) -> int | None:
            cur = c.execute("INSERT OR IGNORE INTO tasks (kind, key, payload, priority, created_at, updated_at, parent_id) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)", (kind, key, dumps(payload), priority, now_iso(), now_iso(), parent_id))
            return cur.lastrowid if cur.rowcount else None
        if db is not None:
            return run(db)
        with self.tx() as c:
            return run(c)

    def claim(self, owner: str, kinds: Iterable[str] | None = None, *, lease: float = 900.0,
              limit: int = 1, same_kind: str | None = None) -> list[sqlite3.Row]:
        kinds = list(kinds) if kinds else None
        with self.tx() as db:
            where = "status = 'pending'"
            args: list[Any] = []
            if same_kind:
                where += " AND kind = ?"
                args.append(same_kind)
            elif kinds:
                where += f" AND kind IN ({','.join('?' * len(kinds))})"
                args.extend(kinds)
            rows = db.execute(f"SELECT id FROM tasks WHERE {where} ORDER BY priority, id LIMIT ?", (*args, limit)).fetchall()
            ids = [r[0] for r in rows]
            if not ids:
                return []
            # every claim is a new generation: a finish or release from an older claim of the same
            # task (a list parked, woken and claimed again meanwhile) no longer matches
            db.execute(f"UPDATE tasks SET status = 'running', lease_owner = ?, lease_until = ?, attempts = attempts + 1, "
                       f"lease_gen = lease_gen + 1, updated_at = ? WHERE id IN ({','.join('?' * len(ids))})", (owner, time.time() + lease, now_iso(), *ids))
            return db.execute(f"SELECT * FROM tasks WHERE id IN ({','.join('?' * len(ids))}) ORDER BY priority, id",
                              ids).fetchall()

    def finish_task(self, task_id: int, status: str, *, result: Any = None, error: str | None = None,
                    db: sqlite3.Connection | None = None, lease: int | None = None) -> bool:
        """Record a task's outcome; True when it was recorded.  ``lease`` is the ``lease_gen`` of the
        claim that ran it: an outcome of an older claim never touches a newer one.  Without
        ``lease`` (callers that do not track claims) a task parked by its handler and claimed again
        since (``progress.parkedLease`` != ``lease_gen``) is likewise left alone."""
        # a review list its handler parked (``waiting``), or one already woken again while its worker
        # was finishing, keeps that state: only its result is recorded
        sql = ("UPDATE tasks SET status = CASE WHEN status = 'waiting' OR (status = 'pending' AND "
               "json_extract(progress, '$.waitDates') = 1) THEN status ELSE ? END, result = COALESCE(?, result), "
               "last_error = ?, lease_owner = NULL, lease_until = NULL, updated_at = ? WHERE id = ? AND "
               + self._LEASE_MATCH)
        args = (status, dumps(result), error, now_iso(), task_id, lease, lease)
        if db is not None:
            return db.execute(sql, args).rowcount > 0
        with self.tx() as c:
            return c.execute(sql, args).rowcount > 0

    #: the claim that ran a task is still its latest one (see finish_task)
    _LEASE_MATCH = ("(lease_gen = ? OR (? IS NULL AND NOT (json_extract(progress, '$.parkedLease') IS NOT NULL "
                    "AND json_extract(progress, '$.parkedLease') != lease_gen)))")

    def release_task(self, task_id: int, *, error: str | None, back_to: str = "pending",
                     lease: int | None = None) -> bool:
        with self.tx() as db:
            return db.execute("UPDATE tasks SET status = ?, last_error = ?, lease_owner = NULL, lease_until = NULL, "
                              "updated_at = ? WHERE id = ? AND " + self._LEASE_MATCH,
                              (back_to, error, now_iso(), task_id, lease, lease)).rowcount > 0

    def save_progress(self, task_id: int, progress: dict, db: sqlite3.Connection) -> None:
        db.execute("UPDATE tasks SET progress = ?, updated_at = ?, lease_until = ? WHERE id = ?",
                   (dumps(progress), now_iso(), time.time() + 900, task_id))

    def recover_stale(self) -> int:
        """Tasks left ``running`` by a dead process go back to ``pending``."""
        with self.tx() as db:
            cur = db.execute("UPDATE tasks SET status = 'pending', lease_owner = NULL, lease_until = NULL, "
                             "last_error = COALESCE(last_error, 'interrupted') WHERE status = 'running'")
            return cur.rowcount

    def retry_failed(self, kind: str = "all") -> int:
        with self.tx() as db:
            if kind == "all":
                cur = db.execute("UPDATE tasks SET status = 'pending', attempts = 0, updated_at = ? WHERE status = 'failed'",
                                 (now_iso(),))
            else:
                cur = db.execute("UPDATE tasks SET status = 'pending', attempts = 0, updated_at = ? "
                                 "WHERE status = 'failed' AND kind = ?", (now_iso(), kind))
            n = cur.rowcount
            if kind in ("all", "reviews", "review_dates"):
                # reviews whose Maps date reads were given up get their attempts back, and their
                # finished date tasks run again (the handler skips reviews already resolved)
                db.execute("UPDATE review_dates SET attempts = 0 WHERE observed_at IS NULL OR status != 'ok'")
                n += db.execute("UPDATE tasks SET status = 'pending', attempts = 0, updated_at = ? "
                                "WHERE kind = 'review_dates' AND status = 'done'", (now_iso(),)).rowcount
            if kind in ("all", "reviews"):
                # reviews cut short by the session (paging stalled, preview only, sort refused) are
                # session properties, not results: they are paged again from the start
                fids = [r[0] for r in db.execute(
                    "SELECT fid FROM sections WHERE section = 'reviews' AND ((status = 'limited' AND "
                    "(reason IN ('no_new_pages', 'refused') OR reason LIKE 'Google served only a preview%' "
                    "OR reason LIKE 'Google''s % list ended at %' OR reason LIKE 'the reviews do not fit into one browser session%')) "
                    "OR reason LIKE '%refused the sort%')")]
                for fid in fids:
                    n += db.execute("UPDATE tasks SET status = 'pending', attempts = 0, progress = NULL, updated_at = ? "
                                    "WHERE kind = 'reviews' AND status = 'done' AND json_extract(payload, '$.fid') = ?",
                                    (now_iso(), fid)).rowcount
            return n

    def task_counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for row in self.db.execute("SELECT kind, status, COUNT(*) FROM tasks GROUP BY kind, status"):
            out.setdefault(row[0], {})[row[1]] = row[2]
        return out

    def pending_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('pending', 'running')").fetchone()[0]

    # --------------------------------------------------------------- places
    def resolve_alias(self, alias: str) -> str | None:
        row = self.db.execute("SELECT fid FROM place_aliases WHERE alias = ?", (alias,)).fetchone()
        return row[0] if row else None

    def upsert_place(self, db: sqlite3.Connection, fid: str, fields: dict[str, Any], *, source: str,
                     language: str, parser_version: str, errors: dict | None = None) -> None:
        """Merge a parsed place.  ``source`` 'search' fills ``search_data``; 'details' fills ``details``."""
        row = db.execute("SELECT fid FROM places WHERE fid = ?", (fid,)).fetchone()
        loc = fields.get("location") or {}
        common = (fields.get("placeId"), fields.get("cid"), fields.get("kgmid"), fields.get("title"),
                  loc.get("lat"), loc.get("lng"))
        if row is None:
            db.execute("INSERT INTO places (fid, place_id, cid, kgmid, title, lat, lng, search_data, details, details_at, "
                       "language, parser_version, first_seen_at, updated_at, parse_errors) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (fid, *common, dumps(fields) if source == "search" else None,
                        dumps(fields) if source == "details" else None, now_iso() if source == "details" else None,
                        language, parser_version, now_iso(), now_iso(), dumps(errors or None)))
        else:
            col = "search_data" if source == "search" else "details"
            extra = ", details_at = ?" if source == "details" else ""
            args: list[Any] = [*common, dumps(fields), language, parser_version, now_iso(), dumps(errors or None)]
            if source == "details":
                args.append(now_iso())
            db.execute(f"UPDATE places SET place_id = COALESCE(?, place_id), cid = COALESCE(?, cid), "
                       f"kgmid = COALESCE(?, kgmid), title = COALESCE(?, title), lat = COALESCE(?, lat), "
                       f"lng = COALESCE(?, lng), {col} = ?, language = ?, parser_version = ?, updated_at = ?, "
                       f"parse_errors = ?{extra} WHERE fid = ?", (*args, fid))
        for alias, kind in ((fid, "fid"), (fields.get("placeId"), "place_id"), (fields.get("cid"), "cid"),
                            (fields.get("kgmid"), "kgmid")):
            if alias:
                db.execute("INSERT OR IGNORE INTO place_aliases (alias, kind, fid) VALUES (?, ?, ?)", (alias, kind, fid))

    def place(self, fid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM places WHERE fid = ?", (fid,)).fetchone()

    def add_discovery(self, db: sqlite3.Connection, **row: Any) -> int | None:
        cols = list(row.keys()) + ["created_at"]
        cur = db.execute(f"INSERT OR IGNORE INTO discoveries ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         (*row.values(), now_iso()))
        return cur.lastrowid if cur.rowcount else None

    def add_result(self, db: sqlite3.Connection, **row: Any) -> bool:
        """Record that ``fid`` belongs to the output of ``search_key``; False if already there."""
        cols = list(row.keys()) + ["created_at"]
        cur = db.execute(f"INSERT OR IGNORE INTO results ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         (*row.values(), now_iso()))
        return bool(cur.rowcount)

    def accepted_count(self, search_key: str, db: sqlite3.Connection | None = None) -> int:
        c = db or self.db
        return c.execute("SELECT COUNT(*) FROM results WHERE search_key = ? AND accepted = 1", (search_key,)).fetchone()[0]

    def total_accepted(self, db: sqlite3.Connection | None = None) -> int:
        c = db or self.db
        return c.execute("SELECT COUNT(DISTINCT fid) FROM results WHERE accepted = 1").fetchone()[0]

    # ------------------------------------------------------------- sections
    def set_section(self, fid: str, section: str, status: str, *, reason: str | None = None,
                    count: int | None = None, expected: int | None = None, cursor: str | None = None,
                    detail: Any = None, db: sqlite3.Connection | None = None) -> None:
        sql = ("INSERT INTO sections (fid, section, status, reason, count, expected, cursor, detail, updated_at) "
               "VALUES (?, ?, ?, ?, COALESCE(?, 0), ?, ?, ?, ?) ON CONFLICT (fid, section) DO UPDATE SET "
               "status = excluded.status, reason = excluded.reason, count = COALESCE(?, sections.count), "
               "expected = COALESCE(excluded.expected, sections.expected), cursor = excluded.cursor, "
               "detail = COALESCE(excluded.detail, sections.detail), updated_at = excluded.updated_at")
        args = (fid, section, status, reason, count, expected, cursor, dumps(detail), now_iso(), count)
        if db is not None:
            db.execute(sql, args)
        else:
            with self.tx() as c:
                c.execute(sql, args)

    def section(self, fid: str, section: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM sections WHERE fid = ? AND section = ?", (fid, section)).fetchone()

    # -------------------------------------------------------------- reviews
    def add_review_page(self, db: sqlite3.Connection, fid: str, sort: str, query: str, page: int,
                        request_cursor: str | None, next_cursor: str | None, reviews: list[dict],
                        parser_version: str, attempt: int | None = None) -> int:
        """Store one page of reviews and its cursor in the caller's transaction; returns new ids.

        ``attempt`` numbers the reads of a place's list (a new session reads it from the top);
        without it a first page (no request cursor) starts a new attempt.  A review seen again
        takes the order, attempt, page and position of this read, so an export ordered by
        (attempt desc, page, position) follows the latest read of the list and continues with
        the deeper part known only from earlier reads."""
        if attempt is None:
            last = db.execute("SELECT COALESCE(MAX(attempt), 0) FROM reviews WHERE fid = ?", (fid,)).fetchone()[0]
            attempt = last + 1 if request_cursor is None else max(last, 1)
        new = 0
        for pos, review in enumerate(reviews):
            cur = db.execute("INSERT OR IGNORE INTO reviews (fid, review_id, data, published_at, stars, sort, query, page, "
                             "position, fetched_at, parser_version, attempt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (fid, review["reviewId"], dumps(review), review.get("publishedAtDate"), review.get("stars"),
                              sort, query, page, pos, now_iso(), parser_version, attempt))
            new += cur.rowcount
            if not cur.rowcount:
                # seen before (in an earlier read or under a fallback order): it belongs to this read
                db.execute("UPDATE reviews SET data = ?, published_at = ?, stars = ?, sort = ?, query = ?, page = ?, "
                           "position = ?, attempt = ?, fetched_at = ?, parser_version = ? WHERE fid = ? AND review_id = ?",
                           (dumps(review), review.get("publishedAtDate"), review.get("stars"), sort, query, page, pos,
                            attempt, now_iso(), parser_version, fid, review["reviewId"]))
        db.execute("INSERT OR REPLACE INTO review_pages (fid, sort, query, page, request_cursor, next_cursor, ids, "
                   "new_ids, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (fid, sort, query, page, request_cursor, next_cursor, dumps([r["reviewId"] for r in reviews]), new,
                    now_iso()))
        return new

    def add_search_page(self, db: sqlite3.Connection, fid: str, sort: str, query: str, page: int,
                        request_cursor: str | None, next_cursor: str | None, rows: list[dict],
                        parser_version: str) -> int:
        """Store one page of the Search list and its cursor in the caller's transaction.

        ``rows``: {"review": Apify fields without dates, "listTimeMs", "ownerHash", "reviewUrl",
        "origin"}.  The review data never carries a date from Search: exact dates live in
        ``review_dates`` and are merged at export.  A review seen again takes this read's place."""
        new = self.put_search_rows(db, fid, sort, query, page, rows, parser_version)
        db.execute("INSERT OR REPLACE INTO review_pages (fid, sort, query, page, request_cursor, next_cursor, ids, "
                   "new_ids, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (fid, sort, query, page, request_cursor, next_cursor,
                    dumps([r["review"]["reviewId"] for r in rows]), new, now_iso()))
        return new

    def put_search_rows(self, db: sqlite3.Connection, fid: str, sort: str, query: str, page: int, rows: list[dict],
                        parser_version: str) -> int:
        """Search rows (add_search_page's ``rows``) stored under a list in the caller's transaction; returns
        how many were new."""
        new = 0
        for pos, row in enumerate(rows):
            review = row["review"]
            vals = (dumps(review), review.get("stars"), sort, query, page, pos, now_iso(), parser_version,
                    row.get("listTimeMs"), row.get("ownerHash"), row.get("reviewUrl"), row.get("origin"))
            cur = db.execute("INSERT OR IGNORE INTO reviews (fid, review_id, data, published_at, stars, sort, query, page, "
                             "position, fetched_at, parser_version, attempt, source, list_time_ms, owner_hash, review_url, "
                             "origin) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 1, 'search', ?, ?, ?, ?)",
                             (fid, review["reviewId"], *vals))
            new += cur.rowcount
            if not cur.rowcount:
                db.execute("UPDATE reviews SET data = ?, published_at = NULL, stars = ?, sort = ?, query = ?, page = ?, "
                           "position = ?, fetched_at = ?, parser_version = ?, attempt = 1, source = 'search', "
                           "list_time_ms = ?, owner_hash = ?, review_url = ?, origin = ? WHERE fid = ? AND review_id = ?",
                           (*vals, fid, review["reviewId"]))
        return new

    def add_pool_rows(self, db: sqlite3.Connection, fid: str, list_order: str, page: int, rows: list[dict],
                      parser_version: str) -> None:
        """Every Search record of a page read to look up a Travel list's Google ids (``rows`` as for
        add_search_page), in the caller's transaction; a record seen again takes this read's place."""
        now = now_iso()
        for row in rows:
            db.execute("INSERT OR REPLACE INTO search_pool (fid, review_id, list_order, page, list_time_ms, data, fetched_at, "
                       "parser_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (fid, row["review"]["reviewId"], list_order, page, row.get("listTimeMs"), dumps(row), now,
                        parser_version))

    def pool_rows(self, fid: str, ids: Iterable[str], db: sqlite3.Connection | None = None) -> dict[str, dict]:
        """The pooled Search rows of these ids (id -> add_search_page row)."""
        want = list(dict.fromkeys(ids))
        out: dict[str, dict] = {}
        for i in range(0, len(want), 500):
            chunk = want[i:i + 500]
            for r in (db or self.db).execute(f"SELECT review_id, data FROM search_pool WHERE fid = ? AND review_id IN "
                                             f"({', '.join('?' * len(chunk))})", (fid, *chunk)):
                out[r["review_id"]] = loads(r["data"])
        return out

    def add_travel_page(self, db: sqlite3.Connection, fid: str, sort: str, query: str, page: int,
                        request_cursor: str | None, next_cursor: str | None, entries: list[dict],
                        parser_version: str, repeated: int = 0) -> int:
        """Store one page of Google's Travel list (maps/travel_reviews.py) in the caller's transaction:
        the order of its records (Google and partner, by source + id), the partner records, and the
        page with its cursors.  ``entries``: {"origin": "google" | partner name, "reviewId", and for a
        partner "review" (Apify fields), "sourceId", "rating", "scale", "relative", "dayDate",
        "daySource", "partnerUrl"}.  A record seen on an earlier page keeps its first position."""
        start = db.execute("SELECT COALESCE(MAX(position) + 1, 0) FROM travel_order WHERE fid = ? AND sort = ? AND query = ?",
                           (fid, sort, query)).fetchone()[0]
        new, now = 0, now_iso()
        for e in entries:
            # a Google record keeps Travel's own fields ("stub"): used only if no Search list shows the review
            cur = db.execute("INSERT OR IGNORE INTO travel_order (fid, sort, query, position, origin, review_id, page, fetched_at, "
                             "data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (fid, sort, query, start + new, e["origin"],
                                                                         e["reviewId"], page, now, dumps(e.get("stub"))))
            new += cur.rowcount
            if e["origin"] != "google":
                db.execute("INSERT INTO partner_reviews (fid, origin, review_id, source_id, data, rating, scale, relative_time, "
                           "day_date, day_source, partner_url, sort, query, fetched_at, parser_version) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (fid, origin, review_id) DO UPDATE SET "
                           "source_id = excluded.source_id, data = excluded.data, rating = excluded.rating, "
                           "scale = excluded.scale, relative_time = excluded.relative_time, "
                           "day_date = COALESCE(excluded.day_date, partner_reviews.day_date), "
                           "day_source = COALESCE(excluded.day_source, partner_reviews.day_source), "
                           "partner_url = excluded.partner_url, sort = excluded.sort, query = excluded.query, "
                           "fetched_at = excluded.fetched_at, parser_version = excluded.parser_version",
                           (fid, e["origin"], e["reviewId"], e.get("sourceId"), dumps(e["review"]), e.get("rating"),
                            e.get("scale"), e.get("relative"), e.get("dayDate"), e.get("daySource"), e.get("partnerUrl"),
                            sort, query, now, parser_version))
        db.execute("INSERT OR REPLACE INTO travel_pages (fid, sort, query, page, request_cursor, next_cursor, records, new_ids, "
                   "repeated, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (fid, sort, query, page, request_cursor, next_cursor, len(entries), new, repeated, now))
        return new

    def travel_order(self, fid: str, sort: str, query: str, db: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
        return (db or self.db).execute("SELECT * FROM travel_order WHERE fid = ? AND sort = ? AND query = ? ORDER BY position",
                                       (fid, sort, query)).fetchall()

    def review_dates_row(self, fid: str, review_id: str, db: sqlite3.Connection | None = None) -> sqlite3.Row | None:
        return (db or self.db).execute("SELECT * FROM review_dates WHERE fid = ? AND review_id = ?",
                                       (fid, review_id)).fetchone()

    def put_review_dates(self, db: sqlite3.Connection, fid: str, review_id: str, **fields: Any) -> None:
        """Insert or update a review's dates row (only the given columns change)."""
        fields["updated_at"] = now_iso()
        cols = list(fields)
        db.execute(f"INSERT INTO review_dates (fid, review_id, {', '.join(cols)}) VALUES (?, ?, {', '.join('?' * len(cols))}) "
                   f"ON CONFLICT (fid, review_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols)}",
                   (fid, review_id, *fields.values()))

    def next_review_attempt(self, fid: str) -> int:
        return self.db.execute("SELECT COALESCE(MAX(attempt), 0) + 1 FROM reviews WHERE fid = ?", (fid,)).fetchone()[0]

    def review_count(self, fid: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM reviews WHERE fid = ?", (fid,)).fetchone()[0]

    def reviews_for(self, fid: str) -> list[dict]:
        rows = self.db.execute("SELECT data FROM reviews WHERE fid = ? ORDER BY sort, attempt DESC, page, position", (fid,)).fetchall()
        return [loads(r[0]) for r in rows]

    # --------------------------------------------------------------- images
    def add_images(self, db: sqlite3.Connection, fid: str, photos: list[dict], start: int) -> int:
        new = 0
        for i, photo in enumerate(photos):
            cur = db.execute("INSERT OR IGNORE INTO images (fid, photo_id, position, data, fetched_at) VALUES (?, ?, ?, ?, ?)",
                             (fid, photo["photoId"], start + i, dumps(photo), now_iso()))
            new += cur.rowcount
        return new

    def update_image(self, fid: str, photo_id: str, data: dict) -> None:
        with self.tx() as db:
            db.execute("UPDATE images SET data = ? WHERE fid = ? AND photo_id = ?", (dumps(data), fid, photo_id))

    # --------------------------------------------------------------- extras
    def put_extra(self, db: sqlite3.Connection, fid: str, kind: str, data: Any, *, source_url: str | None,
                  parser_version: str, evidence_sha: str | None) -> None:
        """A place's enriched section (``orderOnline``, ``reservation``), replaced by its latest read."""
        db.execute("INSERT INTO extras (fid, kind, data, source_url, observed_at, parser_version, evidence_sha) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (fid, kind) DO UPDATE SET data = excluded.data, "
                   "source_url = excluded.source_url, observed_at = excluded.observed_at, "
                   "parser_version = excluded.parser_version, evidence_sha = excluded.evidence_sha",
                   (fid, kind, dumps(data), source_url, now_iso(), parser_version, evidence_sha))

    def extra(self, fid: str, kind: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM extras WHERE fid = ? AND kind = ?", (fid, kind)).fetchone()

    def images_for(self, fid: str) -> list[dict]:
        rows = self.db.execute("SELECT data FROM images WHERE fid = ? ORDER BY position", (fid,)).fetchall()
        return [loads(r[0]) for r in rows]

    # ------------------------------------------------------------- sessions
    def session_opened(self, session_id: str, worker: str, options: str) -> None:
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO sessions (session_id, worker, options, created_at, status) "
                       "VALUES (?, ?, ?, ?, 'open')", (session_id, worker, options, now_iso()))

    def session_closed(self, session_id: str, info: dict, *, commands: int, busy: float, error: str | None = None) -> None:
        billing = info.get("billing") or {}
        status = str(info.get("status") or "")
        # only a terminal state reported by bro counts as closed; anything else is re-checked later
        status = status if status in TERMINAL_SESSION_STATES else "stop_pending"
        with self.tx() as db:
            db.execute("UPDATE sessions SET closed_at = ?, status = ?, billing = ?, total_billed = ?, commands = ?, "
                       "busy_seconds = ?, proxy_usage = ?, error = COALESCE(?, error) WHERE session_id = ?",
                       (now_iso() if status != "stop_pending" else None, status, dumps(billing),
                        billing.get("total_billed"), commands, round(busy, 1), dumps(info.get("proxy_usage")), error,
                        session_id))

    def open_sessions(self) -> list[str]:
        """Sessions not confirmed terminal by bro: open ones and closes that were never confirmed."""
        marks = ",".join("?" * len(TERMINAL_SESSION_STATES))
        return [r[0] for r in self.db.execute(f"SELECT session_id FROM sessions WHERE status NOT IN ({marks})",
                                              tuple(TERMINAL_SESSION_STATES))]

    def billing_total(self) -> float:
        return float(self.db.execute("SELECT COALESCE(SUM(total_billed), 0) FROM sessions").fetchone()[0])
