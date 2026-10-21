"""Job lifecycle: start, resume, stop, finalise."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .. import APIFY_CONTRACT_BUILD, __version__
from ..bro.transport import BroTransport
from ..geo.geometry import Area
from ..input_model import (DISCOVERY_ALGORITHMS, OPTIMIZED, RunInput, parse_input, pin_contract, pin_discovery,
                           resolve_relative_start)
from ..maps import photos, place, reviews, search
from ..store import Store, loads, now_iso
from .context import RunContext, Territory
from .planner import PlanSummary, plan
from .worker import Worker

log = logging.getLogger(__name__)


def code_manifest() -> dict[str, Any]:
    """SHA-256 over the package's own files (sources and contracts, by relative path): the exact code
    a job ran, whether installed from a wheel or run from a checkout."""
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".json", ".md")
                   and "__pycache__" not in p.parts)
    for p in files:
        h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return {"sha256": h.hexdigest(), "files": len(files)}


def versions() -> dict[str, Any]:
    """What a job ran: package, code manifest, dependencies and the parser of each kind of record."""
    from importlib import metadata

    from ..enrich.website import PARSER_VERSION as CONTACTS
    from ..maps import order_online, reservation, search_reviews, travel_reviews
    from . import review_dates, review_filter, extras

    deps = {}
    for name in ("bro-api-sdk", "pydantic", "requests", "shapely", "pyproj"):
        try:
            deps[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            deps[name] = None
    return {"package": __version__, "apifyContract": APIFY_CONTRACT_BUILD, "code": code_manifest(),
            "python": platform.python_version(), "platform": platform.system(), "dependencies": deps,
            "place": place.PARSER_VERSION, "search": search.PARSER_VERSION,
            "reviewList": search_reviews.PARSER_VERSION, "reviewDates": review_dates.PARSER_VERSION,
            "reviewRecord": reviews.PARSER_VERSION, "photos": photos.PARSER_VERSION, "contacts": CONTACTS,
            "reviewFilter": review_filter.ALGORITHM, "orderOnline": order_online.PARSER_VERSION,
            "reservation": reservation.PARSER_VERSION, "webResults": extras.PARSER_VERSION,
            "travelReviews": travel_reviews.PARSER_VERSION, "discoveryAlgorithms": list(DISCOVERY_ALGORITHMS)}


def stored_input(job: Any) -> dict[str, Any]:
    """A job's input for running it, with every discovery setting and the collection contract the input leaves unset (absent or
    null) taken from the values the job was normalised with -- also when the strategy itself was
    given: an rc14-rc17 job with an explicit adaptive-v3 and no knobs ran 0.4 / 40, not the later
    preset.  A strategy known nowhere is legacy-v1 (jobs from before the setting existed).  So a
    later default or preset never changes the traversal of a resumed job."""
    raw = loads(job["input_json"]) or {}
    bro = raw.get("bro") or {}
    normalized = (loads(job["normalized_json"]) or {}).get("bro") or {}
    algorithm = bro.get("discoveryAlgorithm") or normalized.get("discoveryAlgorithm") or "legacy-v1"
    keys = ("minCellKm", "saturationThreshold") + (("discoveryFetchMode",) if algorithm in OPTIMIZED else ())
    # the collection contract likewise: a job from before the setting is rc27 (Search-only reviews,
    # local filter), whatever the current default
    contract = bro.get("collectionContract") or normalized.get("collectionContract") or "rc27"
    filter_mode = (bro.get("reviewsFilterMode") or normalized.get("reviewsFilterMode")
                   or ("local_phrase" if contract == "rc27" else "google"))
    filled = {"discoveryAlgorithm": algorithm, "collectionContract": contract, "reviewsFilterMode": filter_mode,
              **{k: normalized[k] for k in keys if bro.get(k) is None and normalized.get(k) is not None}}
    if all(bro.get(k) == v for k, v in filled.items()):
        return raw
    return {**raw, "bro": {**bro, **filled}}


def load_env(path: Path | None = None) -> None:
    for candidate in [path] if path else [Path.cwd() / ".env", Path(__file__).resolve().parents[3] / ".env"]:
        if candidate and candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            return


class JobBusy(RuntimeError):
    """Another process is running this job."""


class JobLock:
    """One process per job directory: an exclusive OS lock on ``run.lock``.

    The lock is taken atomically (``msvcrt.locking`` / ``fcntl.flock``) and held by the open
    file handle, so the operating system frees it when the process ends, also on a crash or
    power loss; there is no check-then-write window and no stale-lock takeover.  ``run.owner``
    only describes the holder for the error message.  Without the lock a second ``resume``
    would return the first process's running tasks to the queue and stop its sessions."""

    def __init__(self, job_dir: Path) -> None:
        self.path = Path(job_dir) / "run.lock"
        self.owner = Path(job_dir) / "run.owner"
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> None:
        if self._fh is not None:
            return
        fh = open(self.path, "a+b")
        try:
            _os_lock(fh)
        except OSError:
            fh.close()
            try:
                who = self.owner.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                who = "unknown process"
            raise JobBusy(f"the job is being run by another process ({who})") from None
        self._fh = fh
        try:
            self.owner.write_text(f"pid {os.getpid()} since {now_iso()}", encoding="utf-8")
        except OSError:
            pass

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _os_unlock(fh)
        except OSError:
            pass
        fh.close()

    def __enter__(self) -> "JobLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


if os.name == "nt":
    import msvcrt

    def _os_lock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _os_unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _os_lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _os_unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class JobRunner:
    def __init__(self, job_dir: str | Path, *, api_key: str | None = None,
                 transport_factory: Callable[[Store], BroTransport] | None = None):
        self.job_dir = Path(job_dir)
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.job_dir / "state.sqlite")
        self.api_key = api_key
        self.transport_factory = transport_factory
        self.ctx: RunContext | None = None
        self.job_lock = JobLock(self.job_dir)

    def lock(self) -> "JobRunner":
        """Take the job's lock before changing the job (JobBusy when another process runs it)."""
        self.job_lock.acquire()
        return self

    # -------------------------------------------------------------- setup
    def _transport(self) -> BroTransport:
        if self.transport_factory:
            return self.transport_factory(self.store)
        key = self.api_key or os.environ.get("BRO_API_KEY")
        store = self.store

        def on_event(kind: str, data: dict[str, Any]) -> None:
            store.event(kind, kind.replace("_", " "), data=data)

        return BroTransport(key or "", on_event=on_event)

    def _load_territories(self, ctx: RunContext) -> None:
        from shapely.geometry import shape
        for row in self.store.db.execute("SELECT * FROM territories ORDER BY id"):
            geom = shape(loads(row["geojson"]))
            area = Area(geometry=geom, source=row["source"], label=row["label"], geojson=loads(row["geojson"]),
                        provenance=loads(row["provenance"]) or {})
            ctx.territories.append(Territory(row["id"], area))

    def prepare(self, run: RunInput) -> PlanSummary | None:
        """Create the job (once) and plan it; a second call on the same directory is a no-op."""
        job = self.store.job()
        if job is not None and job["status"] != "created":
            raise RuntimeError(f"{self.job_dir} already holds a job (status {job['status']}); use resume")
        if job is None:
            fixed = resolve_relative_start(run.raw, run)
            if fixed is not run.raw:
                run.warnings.append(f"reviewsStartDate {run.raw['reviewsStartDate']!r} is fixed for this job as "
                                    f"{fixed['reviewsStartDate']} (resumes keep this cutoff)")
            fixed = pin_discovery(fixed, run)   # resumes keep this traversal, whatever a later default
            fixed = pin_contract(fixed, run)    # and this collection contract
            self.store.init_job(fixed, {"apify": run.apify, "bro": run.bro.model_dump(), "warnings": run.warnings,
                                          "legacy": run.legacy}, versions())
        ctx = RunContext(job_dir=self.job_dir, store=self.store, run=run, transport=None)
        summary = plan(ctx)
        self.store.set_job(status="planned")
        self.store.event("plan", "job planned", data={"tasks": summary.tasks, "territories": summary.territories,
                                                      "warnings": summary.warnings})
        return summary

    def _context(self) -> RunContext:
        job = self.store.job()
        if job is None:
            raise RuntimeError(f"no job in {self.job_dir}")
        run = parse_input(stored_input(job))
        ctx = RunContext(job_dir=self.job_dir, store=self.store, run=run, transport=self._transport())
        self._load_territories(ctx)
        return ctx

    # ---------------------------------------------------------------- run
    def run(self, *, on_progress: Callable[[RunContext], None] | None = None) -> dict[str, Any]:
        self.job_lock.acquire()
        try:
            return self._run_locked(on_progress)
        finally:
            self.job_lock.release()

    def _run_locked(self, on_progress: Callable[[RunContext], None] | None) -> dict[str, Any]:
        ctx = self._context()
        self.ctx = ctx
        store = self.store
        recovered = store.recover_stale()
        if recovered:
            store.event("resume", f"{recovered} interrupted task(s) returned to the queue")
        self._stop_leftover_sessions(ctx)
        if ctx.run.bro.reviewsMode == "exact":
            from .review_dates import sweep
            queued = sweep(ctx)
            if queued:
                store.event("resume", f"{queued} review-date task(s) queued for reviews still without exact dates")
        store.set_job(status="running", started_at=now_iso(), stop_requested=0, stop_reason=None)
        # every run (the first and each resume) records the code it runs: a job resumed on newer code
        # keeps the history, job.versions describes the latest run
        current = versions()
        with store.tx() as db:
            db.execute("UPDATE job SET runs = runs + 1 WHERE id = 1")
            row = db.execute("SELECT runs, versions FROM job WHERE id = 1").fetchone()
            old = loads(row[1]) or {}
            history = old.get("byRun") or ([{"run": 0, "versions": {k: v for k, v in old.items() if k != "byRun"}}]
                                           if old and "code" not in old else [])
            history.append({"run": row[0], "startedAt": now_iso(), "versions": current})
            db.execute("UPDATE job SET versions = ? WHERE id = 1", (json.dumps({**current, "byRun": history}),))
        store.event("run_start", "run started", data={
            "acceptedPlaces": store.total_accepted(),
            "reviews": store.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
            "billedUsd": store.billing_total(), "concurrency": ctx.run.bro.concurrency, "version": __version__,
            "code": current["code"]["sha256"]})
        try:
            account = ctx.transport.account()
            store.event("account", "bro account checked",
                        data={k: account.get(k) for k in ("pricing_tier", "max_sessions", "active_sessions", "balance")})
            limit = int(account.get("max_sessions") or ctx.run.bro.concurrency)
        except Exception as exc:  # noqa: BLE001 - an unreadable account is a hard stop
            store.set_job(status="failed", finished_at=now_iso(), stop_reason=f"bro account: {exc}")
            raise
        concurrency = max(1, min(ctx.run.bro.concurrency, limit))
        workers = [Worker(ctx, i) for i in range(concurrency)]
        previous = self._install_signals(ctx)
        try:
            for w in workers:
                w.start()
                time.sleep(1.0)
            while any(w.is_alive() for w in workers):
                for w in workers:
                    w.join(timeout=5.0)
                if on_progress:
                    on_progress(ctx)
        finally:
            self._restore_signals(previous)
        # a review list parked for dates counts as unfinished work: never "completed" while one waits
        pending = store.pending_count() + store.db.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'waiting'").fetchone()[0]
        if ctx.stop_reason in ("worker_crash",) or (ctx.stop_reason or "").startswith("bro_auth"):
            status = "failed"
        elif pending:
            status = "stopped"
        elif store.db.execute("SELECT COUNT(*) FROM tasks WHERE status = 'failed'").fetchone()[0]:
            # the queue is empty, but some work ended in failure: not a success
            status = "completed_with_errors"
        else:
            status = "completed"
        if status in ("stopped", "failed"):
            # sections whose task went back to the queue keep what was collected; until a resume
            # they are reported as limited by the stop, not as still running
            with store.tx() as db:
                db.execute("UPDATE sections SET status = 'limited', reason = ?, updated_at = ? WHERE status = 'running'",
                           (f"run {status}: {ctx.stop_reason or 'unknown'}; resume continues", now_iso()))
        store.set_job(status=status, finished_at=now_iso(), stop_reason=ctx.stop_reason)
        summary = {"status": status, "stopReason": ctx.stop_reason, "pendingTasks": pending,
                   "billing": store.billing_total(), "workers": [{"name": w.name, "tasks": w.tasks_done,
                                                                    "error": w.error} for w in workers]}
        store.event("run", f"run ended: {status}", data=summary)
        return summary

    def reconcile(self) -> dict[str, Any]:
        """Bring the local record of a job whose process died in line with bro, without
        collecting anything: sessions not confirmed closed are stopped (idempotent) and get bro's
        final status and billing; a job left ``running`` becomes ``stopped`` (process_lost), its
        running tasks return to the queue and running sections are reported as limited.
        Call it with the job lock held (:meth:`lock`)."""
        transport = self._transport()
        sessions = []
        for sid in self.store.open_sessions():
            try:
                info = transport.stop_and_bill(sid)
            except Exception as exc:  # noqa: BLE001
                info = {"status": "unknown"}
                log.warning("could not read session %s: %s", sid[:8], exc)
            self.store.session_closed(sid, info, commands=0, busy=0.0, error="reconciled after the process ended")
            billed = (info.get("billing") or {}).get("total_billed")
            sessions.append({"session": sid, "status": info.get("status"), "billed": billed})
            self.store.event("session", f"reconciled session {sid}: {info.get('status')}", data=sessions[-1])
        job = self.store.job()
        changed = None
        if job is not None and job["status"] == "running":
            requeued = self.store.recover_stale()
            with self.store.tx() as db:
                db.execute("UPDATE sections SET status = 'limited', reason = ?, updated_at = ? WHERE status = 'running'",
                           ("run stopped: process_lost; resume continues", now_iso()))
            self.store.set_job(status="stopped", finished_at=now_iso(), stop_reason="process_lost")
            changed = {"from": "running", "to": "stopped", "requeuedTasks": requeued}
            self.store.event("run", "job reconciled after its process ended", data=changed)
        return {"sessions": sessions, "job": changed, "billing": self.store.billing_total()}

    def _stop_leftover_sessions(self, ctx: RunContext) -> None:
        for sid in self.store.open_sessions():
            try:
                info = ctx.transport.stop_and_bill(sid)
            except Exception as exc:  # noqa: BLE001
                info = {"status": "unknown"}
                log.warning("could not stop leftover session %s: %s", sid[:8], exc)
            self.store.session_closed(sid, info, commands=0, busy=0.0, error="left open by a previous process")
            self.store.event("session", f"stopped leftover session {sid}")

    @staticmethod
    def _install_signals(ctx: RunContext):
        if threading.current_thread() is not threading.main_thread():
            return None

        def handler(signum, frame):  # noqa: ARG001
            ctx.request_stop("interrupted")

        previous = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous[sig] = signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        return previous

    @staticmethod
    def _restore_signals(previous) -> None:
        for sig, h in (previous or {}).items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError):
                pass
