"""Shared state of one run: input, store, territories, bro transport and limits."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shapely.ops import unary_union

from ..bro.transport import BroTransport, SessionOptions
from ..geo.geometry import Area
from ..input_model import RunInput
from ..store import Store, now_iso
from .search_cache import SearchResponseCache

log = logging.getLogger(__name__)

#: task kinds and their claim priority (lower runs first)
PRIORITY = {"resolve": 10, "overview": 15, "search": 20, "allplaces": 22, "controlpass": 25, "place": 30, "reviews": 40,
            "review_dates": 42, "images": 45, "reservation": 47, "order_online": 48, "questions": 49, "website": 50,
            "website_early": 41,
            "webresults": 55}

#: task kinds served by review sessions (bro.reviewsProxy): the Search list and the Maps dates
REVIEW_KINDS = ("reviews", "review_dates")

#: country used for Google's ``gl`` when nothing else decides it
DEFAULT_GL = "us"


@dataclass
class Territory:
    id: int
    area: Area


@dataclass
class RunContext:
    job_dir: Path
    store: Store
    run: RunInput
    transport: BroTransport | None
    territories: list[Territory] = field(default_factory=list)
    started_mono: float = field(default_factory=time.monotonic)
    stop_event: threading.Event = field(default_factory=threading.Event)
    stop_reason: str | None = None
    session_replacements: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    blocks: int = 0
    #: sessions dropped because Google gated review sorting behind sign-in
    gated_sessions: int = 0
    #: sessions dropped because a review list stopped serving pages before its end
    stalled_sessions: int = 0
    search_cache: SearchResponseCache = field(default_factory=SearchResponseCache)
    _union: Any = None

    @property
    def gate_budget_exhausted(self) -> bool:
        # the budget is per session slot: three parallel sessions meet three times as many gates
        return self.gated_sessions >= self.run.bro.maxGatedSessions * max(1, self.run.bro.concurrency)

    # --------------------------------------------------------------- locale
    @property
    def hl(self) -> str:
        return self.run.language

    @property
    def gl(self) -> str:
        bro_gl = self.run.bro.gl
        if bro_gl:
            return bro_gl.lower()
        code = self.run.apify.get("countryCode")
        if code:
            return str(code).lower()
        if self.run.bro.proxy.enabled and self.run.bro.proxy.country:
            return self.run.bro.proxy.country.lower()
        return DEFAULT_GL

    def session_options(self, kind: str | None = None) -> SessionOptions:
        p = self.run.bro.proxy
        purpose = ""
        if kind in REVIEW_KINDS:
            purpose = "reviews"
            if self.run.bro.reviewsProxy is not None:
                p = self.run.bro.reviewsProxy
            elif not p.enabled:
                # default for review sessions: a lite proxy in the target country (the Search list
                # endpoint answered on lite and basic, 2026-10-07; anonymous no-proxy VMs were
                # often refused Maps review pages, 2026-10-01/02)
                from ..input_model import BroProxy
                p = BroProxy(enabled=True, tier="lite", policy="basic", country=self.gl.upper())
        return SessionOptions(enable_proxy=p.enabled, proxy_tier=p.tier if p.enabled else None,
                              proxy_policy=p.policy if p.enabled else None, country=p.country, city=p.city,
                              block_unproxied=p.blockUnproxied, session_timeout=self.run.bro.sessionTimeout,
                              purpose=purpose)

    # ----------------------------------------------------------------- area
    @property
    def has_area(self) -> bool:
        return bool(self.territories)

    def inside(self, lat: float | None, lng: float | None) -> bool | None:
        if not self.territories:
            return True
        if lat is None or lng is None:
            return None
        if self._union is None:
            self._union = unary_union([t.area.geometry for t in self.territories])
        from shapely.geometry import Point
        return bool(self._union.covers(Point(lng, lat)))

    # --------------------------------------------------------------- limits
    @property
    def deadline_passed(self) -> bool:
        return time.monotonic() - self.started_mono > self.run.bro.maxRunSeconds

    def should_stop(self) -> bool:
        if self.stop_event.is_set():
            return True
        if self.deadline_passed:
            self.request_stop("time_limit")
            return True
        if self.store.stop_requested():
            self.request_stop("stop_requested")
            return True
        return False

    def request_stop(self, reason: str) -> None:
        with self.lock:
            if not self.stop_event.is_set():
                self.stop_reason = reason
                self.stop_event.set()
                log.warning("stopping: %s", reason)

    # ------------------------------------------------------------- evidence
    def diagnostics(self, name: str, **extra: Any):
        """A writer of JSON lines to ``diagnostics/<name>.jsonl`` (every record gets ``extra``)."""
        folder = self.job_dir / "diagnostics"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.jsonl"

        def write(record: dict) -> None:
            line = json.dumps({"ts": round(time.time(), 3), **extra, **record}, ensure_ascii=False, default=str)
            with self.lock, open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

        return write

    def evidence(self, *, kind: str, url: str, status: int, text: str, task_id: int | None,
                 note: str | None = None, failed: bool = False, keep: bool = False) -> None:
        """Keep a raw answer: always (``evidence=all``), or with ``evidence=errors`` when the read
        failed or the answer is otherwise worth keeping (``keep``: a successful read whose content
        needs its source, such as provider conflicts kept in the data or an empty search page)."""
        mode = self.run.bro.evidence
        if mode == "none" or (mode == "errors" and not (failed or keep)):
            return
        data = (text or "").encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        folder = self.job_dir / "evidence"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{digest[:24]}.txt.gz"
        if not path.exists():
            with gzip.open(path, "wb") as fh:
                fh.write(data)
        with self.store.tx() as db:
            db.execute("INSERT INTO evidence (task_id, kind, url, status, sha256, path, size, note, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (task_id, kind, url[:2000], status, digest, str(path.relative_to(self.job_dir)), len(data),
                        note, now_iso()))
