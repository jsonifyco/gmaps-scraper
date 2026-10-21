"""Workers: one thread per bro session, claiming tasks from the SQLite queue."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any

from ..bro.transport import (BroAuthError, BroBrowser, BroCreateFailed, BroError, BroOutcomeUnknown,
                             BroSessionDead)
from ..maps.client import MapsBlocked, MapsClient, MapsConsentError
from ..store import loads
from .context import RunContext
from .details import handle_places
from .discovery import (NeedFreshSession, NotReady, TaskInterrupted, handle_controlpass, handle_resolve,
                        handle_search)
from .extras import handle_webresults
from .review_dates import after_dates_task, handle_review_dates, wake_orphaned_lists
from .review_list import handle_reviews
from .sections import handle_images

log = logging.getLogger(__name__)

#: rough duration of one task by kind, used to rotate a session before it expires
EXPECTED_SECONDS = {"search": 120, "place": 60, "resolve": 60, "reviews": 300, "review_dates": 400, "images": 180,
                    "website": 240, "webresults": 60, "allplaces": 300, "order_online": 60, "reservation": 60,
                    "questions": 60}
SECTION_OF = {"reviews": "reviews", "review_dates": "reviewDates", "images": "images", "website": "contacts",
              "webresults": "webResults", "order_online": "orderOnline", "reservation": "reservation",
              "questions": "questionsAndAnswers"}
MAX_BLOCKS = 6
IDLE_RELEASE_SECONDS = 120
BROWSERLESS = {"controlpass"}
MAX_CREATE_FAILURES = 4


def _lease(task: Any) -> int | None:
    """The claim generation a task row was claimed with (None for rows from older stores)."""
    try:
        return task["lease_gen"]
    except (IndexError, KeyError):
        return None


def _handler(kind: str):
    if kind == "search":
        return handle_search
    if kind == "resolve":
        return handle_resolve
    if kind == "reviews":
        return handle_reviews
    if kind == "review_dates":
        return handle_review_dates
    if kind == "images":
        return handle_images
    if kind == "webresults":
        return handle_webresults
    if kind == "order_online":
        from .specials import handle_order_online
        return handle_order_online
    if kind == "reservation":
        from .specials import handle_reservation
        return handle_reservation
    if kind == "questions":
        from .questions import handle_questions
        return handle_questions
    if kind == "website":
        from ..enrich.website import handle_website
        return handle_website
    if kind == "allplaces":
        from .allplaces import handle_allplaces
        return handle_allplaces
    if kind == "controlpass":
        return handle_controlpass
    raise ValueError(f"unknown task kind {kind}")


class Worker(threading.Thread):
    def __init__(self, ctx: RunContext, index: int):
        super().__init__(name=f"worker-{index}", daemon=True)
        self.ctx = ctx
        self.index = index
        self.browser: BroBrowser | None = None
        self.maps: MapsClient | None = None
        self.tasks_done = 0
        self.create_failures = 0
        self.error: str | None = None
        self._timings = None

    # ------------------------------------------------------------ browser
    def _open(self, kind: str | None = None) -> None:
        ctx = self.ctx
        if ctx.transport is None:
            raise BroError("no bro transport configured")
        options = ctx.session_options(kind)
        try:
            browser = ctx.transport.open_browser(options, label=self.name)
        except BroCreateFailed:
            self.create_failures += 1
            if self.create_failures >= MAX_CREATE_FAILURES:
                ctx.request_stop("bro_session_create_failures")
            raise
        self.create_failures = 0
        ctx.store.session_opened(browser.session_id, self.name, options.describe())
        ctx.store.event("session", f"{self.name} opened {browser.session_id}", data={"options": options.describe()})
        self.browser = browser
        self.maps = MapsClient(browser, hl=ctx.hl, gl=ctx.gl)

    def _close(self, error: str | None = None) -> None:
        browser, self.browser, self.maps = self.browser, None, None
        if browser is None:
            return
        try:
            info = browser.close()
        except Exception as exc:  # noqa: BLE001 - closing must never mask the original problem
            info = {"status": "unknown"}
            error = f"{error or ''} close failed: {exc}".strip()
        self.ctx.store.session_closed(browser.session_id, info, commands=browser.commands_sent,
                                      busy=browser.busy_seconds, error=error)

    def _replace(self, reason: str) -> None:
        ctx = self.ctx
        self._close(error=reason)
        with ctx.lock:
            ctx.session_replacements += 1
            exhausted = ctx.session_replacements > ctx.run.bro.maxSessionReplacements
        ctx.store.event("session", f"{self.name} replaces its session: {reason}", level="warning")
        if exhausted:
            ctx.request_stop("session_replacements_exhausted")

    def _expected_seconds(self, kind: str, task: Any | None) -> float:
        base = EXPECTED_SECONDS.get(kind, 120)
        if kind == "reviews" and task is not None:
            p = loads(task["payload"]) or {}
            volume = p.get("expected") or 0
            if p.get("limit") is not None:
                volume = min(volume, p["limit"])
            # Search list: 60 reviews per ~2 s request (2026-10-06), plus opening google.com
            base = max(base, min(volume / 30 + 60, self.ctx.run.bro.sessionTimeout - 300))
        elif kind == "review_dates" and task is not None:
            n = len((loads(task["payload"]) or {}).get("ids") or [])
            # ~6 s per single-review page (2026-10-07), plus opening the first page
            base = max(60.0, n * 7 + 60)
        return base

    def _ensure(self, kind: str, task: Any | None = None) -> None:
        if self.browser is not None and self.browser.should_rotate(self._expected_seconds(kind, task)):
            self.ctx.store.event("session", f"{self.name} rotates {self.browser.session_id} before its time limit")
            self._close(error=None)
        wanted = self.ctx.session_options(kind)
        if self.browser is not None and self.browser.options != wanted:
            # review tasks may run on differently routed sessions (bro.reviewsProxy)
            self._close(error=None)
        if self.browser is None:
            self._open(kind)

    # --------------------------------------------------------------- loop
    def run(self) -> None:
        try:
            self._loop()
        except Exception as exc:  # noqa: BLE001 - a crashed worker stops the run with a reason
            self.error = f"{type(exc).__name__}: {exc}"
            log.error("%s crashed: %s\n%s", self.name, exc, traceback.format_exc())
            self.ctx.store.event("worker", f"{self.name} crashed: {exc}", level="error",
                                 data={"traceback": traceback.format_exc()[-4000:]})
            self.ctx.request_stop("worker_crash")
        finally:
            self._close()

    def _claim(self) -> list[Any]:
        store = self.ctx.store
        tasks = store.claim(self.name, limit=1)
        if tasks and tasks[0]["kind"] == "place":
            size = self.ctx.run.bro.placeBatchSize
            if size > 1:
                tasks += store.claim(self.name, limit=size - 1, same_kind="place")
        return tasks

    def _loop(self) -> None:
        ctx = self.ctx
        store = ctx.store
        idle_since: float | None = None
        while True:
            if ctx.should_stop():
                return
            tasks = self._claim()
            if not tasks:
                if store.pending_count() == 0:
                    if wake_orphaned_lists(store):
                        continue
                    return
                idle_since = idle_since or time.monotonic()
                if self.browser is not None and time.monotonic() - idle_since > IDLE_RELEASE_SECONDS:
                    # bro ends sessions idle for 300 s; release ours instead of paying for it
                    self._close(error=None)
                time.sleep(2.0)
                continue
            idle_since = None
            kind = tasks[0]["kind"]
            started = time.monotonic()
            try:
                if kind not in BROWSERLESS:
                    self._ensure(kind, tasks[0])
                self._dispatch(kind, tasks)
            except TaskInterrupted as exc:
                self._requeue(tasks, f"interrupted: {exc}", penalize=False)
            except NotReady as exc:
                self._requeue(tasks, f"waiting: {exc}", penalize=False)
                time.sleep(5.0)
            except NeedFreshSession as exc:
                # a gated session is a property of that VM/IP, not a failure: it is closed and
                # replaced outside the failure budget, within maxGatedSessions; a stalled list
                # is retried a bounded number of times per place and does not use that budget
                kind = getattr(exc, "kind", "gated")
                self._requeue(tasks, f"needs a fresh session ({kind}): {exc}", penalize=False)
                with ctx.lock:
                    if kind == "gated":
                        ctx.gated_sessions += 1
                    else:
                        ctx.stalled_sessions += 1
                    n = ctx.gated_sessions if kind == "gated" else ctx.stalled_sessions
                store.event("session", f"{self.name} drops a {kind} session ({n} in the run)", level="warning")
                self._close(error=f"{kind}: {exc}")
            except MapsBlocked as exc:
                self._requeue(tasks, f"blocked: {exc}", penalize=False)
                with ctx.lock:
                    ctx.blocks += 1
                    too_many = ctx.blocks >= MAX_BLOCKS
                store.event("block", f"{self.name}: {exc}", level="warning")
                if too_many:
                    ctx.request_stop("google_blocks")
                else:
                    self._replace(f"blocked: {exc}")
            except (BroSessionDead, BroOutcomeUnknown, MapsConsentError) as exc:
                self._requeue(tasks, f"{type(exc).__name__}: {exc}")
                self._replace(f"{type(exc).__name__}: {exc}")
            except BroAuthError as exc:
                self._requeue(tasks, str(exc), penalize=False)
                ctx.request_stop(f"bro_auth: {exc}")
                return
            except BroCreateFailed as exc:
                self._requeue(tasks, str(exc), penalize=False)
                store.event("session", f"{self.name}: {exc}", level="warning")
                time.sleep(10)
            except Exception as exc:  # noqa: BLE001 - task-level failure, retried up to maxTaskAttempts
                log.warning("%s task %s failed: %s", self.name, kind, exc)
                self._requeue(tasks, f"{type(exc).__name__}: {exc}"[:500], trace=traceback.format_exc()[-3000:])
            finally:
                self._timing(kind, tasks, started)

    def _dispatch(self, kind: str, tasks: list[Any]) -> None:
        ctx = self.ctx
        if kind == "place":
            outcome = handle_places(ctx, self.maps, tasks)
            retry = [t for t in tasks if outcome.get(t["id"], ("retry", "no outcome"))[0] == "retry"]
            for t in retry:
                self._requeue([t], str(outcome.get(t["id"], ("", "no outcome"))[1]))
            self.tasks_done += len(tasks) - len(retry)
            return
        task = tasks[0]
        result = _handler(kind)(ctx, self.maps, task)
        ctx.store.finish_task(task["id"], "done", result=result, lease=_lease(task))
        self.tasks_done += 1
        if kind == "review_dates":
            after_dates_task(ctx, task)

    def _timing(self, kind: str, tasks: list[Any], started: float) -> None:
        """One line per claim in diagnostics/task-timing.jsonl: kind, wall seconds (session opening
        included) and the outcome -- for the time spent per kind of task."""
        if self._timings is None:
            self._timings = self.ctx.diagnostics("task-timing", worker=self.name)
        ids = [t["id"] for t in tasks]
        marks = ",".join("?" * len(ids))
        states = {r[0]: r[1] for r in self.ctx.store.db.execute(
            f"SELECT status, COUNT(*) FROM tasks WHERE id IN ({marks}) GROUP BY status", ids)}
        self._timings({"kind": kind, "tasks": len(tasks), "seconds": round(time.monotonic() - started, 3),
                       "outcome": states})

    def _requeue(self, tasks: list[Any], error: str, *, penalize: bool = True, trace: str | None = None) -> None:
        store = self.ctx.store
        limit = self.ctx.run.bro.maxTaskAttempts
        for t in tasks:
            row = store.db.execute("SELECT status, attempts, lease_gen FROM tasks WHERE id = ?", (t["id"],)).fetchone()
            if row is None or row["status"] != "running" or (_lease(t) is not None and row["lease_gen"] != _lease(t)):
                continue  # finished, or claimed again by another worker since: not this claim's to retry
            attempts = row["attempts"]
            if not penalize:
                with store.tx() as db:
                    db.execute("UPDATE tasks SET attempts = MAX(attempts - 1, 0) WHERE id = ?", (t["id"],))
                attempts -= 1
            if attempts >= limit:
                store.finish_task(t["id"], "failed", error=error, lease=_lease(t))
                self._fail_section(t, error)
                if t["kind"] == "review_dates":
                    after_dates_task(self.ctx, t)
                store.event("task", f"{t['kind']} {t['key'][:80]} failed after {attempts} attempts: {error}",
                            level="error", data={"trace": trace} if trace else None)
            else:
                store.release_task(t["id"], error=error, lease=_lease(t))

    def _fail_section(self, task: Any, error: str) -> None:
        payload = loads(task["payload"]) or {}
        section = SECTION_OF.get(task["kind"])
        store = self.ctx.store
        if task["kind"] == "place":
            fid = payload.get("fid")
            store.set_section(fid, "details", "failed", reason=error[:300])
        elif section and payload.get("fid"):
            store.set_section(payload["fid"], section, "failed", reason=error[:300])
        elif task["kind"] == "website":
            for (fid,) in store.db.execute("SELECT fid FROM sections WHERE section = 'contacts' AND "
                                           "json_extract(detail, '$.domain') = ?", (payload.get("domain"),)):
                store.set_section(fid, "contacts", "failed", reason=error[:300])
