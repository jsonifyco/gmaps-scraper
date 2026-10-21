"""Exact review dates from Maps single-review pages.

A review's ``reviewUrl`` opens a single-review page.  The page loads the record with its own
signed GET (``/maps/timeline/_rpc/pc?…!10s<fid>!11s<reviewId>``, header ``x-maps-bgkey``), in the
Maps ``qv9Egd`` record layout, so :func:`reviews.parse_review` reads publish, edit and owner-reply
times in microseconds (verified 2026-10-07: 997 of 1 000 reviews of ROW NYC matched the raw Maps
answers of 2026-10-04; the 3 others: one gone from the list, one edited again, one unmatched).

The request is never sent, edited or replayed by us.  The first review is opened with ``open_url``
and a hook keeps the text of every ``pc`` answer the page receives.  Each further review is
opened by the app's own router (``history.pushState`` + ``popstate``, as back/forward or an in-app
link does), and the page requests it as for any review it shows.  Reading the answer from bro's
HAR instead costs ~26 MB / ~14 s per dump (bro files this request with the map's wasm/JS).

Outcomes (``ReviewPageResult.status``):

* ``ok`` -- the record of this review (and this place) arrived;
* ``not_found`` -- the page answered, but without this review's record;
* ``no_answer`` -- no answer for this review within the wait, after one in-app retry;
* ``server_error`` -- Google's own error page (seen: "Error 500", 2 of 12 036 reads on 2026-10-07);
* ``unreadable`` -- any other answer that is not the expected JSON.

bro errors (dead session, unknown command outcome) are not caught here: the worker retires the
session and the task, with the reviews not yet stored, goes back to the queue.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..bro.transport import command

log = logging.getLogger(__name__)

PC_MARK = "timeline/_rpc/pc"

HOOK_JS = r"""(() => {
  if (window.__pcHook) return 'already';
  window.__pcHook = true; window.__pc = {}; window.__pcs = {};
  const keep = (u, t, s) => { const m = /!11s([^!&]+)/.exec(decodeURIComponent(u || '')); if (m) { window.__pc[m[1]] = t; window.__pcs[m[1]] = s; } };
  const F = window.fetch;
  window.fetch = function (input, init) {
    const u = String((input && input.url) || input || '');
    const p = F.apply(this, arguments);
    if (u.includes('timeline/_rpc/pc')) p.then(r => r.clone().text().then(t => keep(u, t, r.status))).catch(() => {});
    return p;
  };
  const XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__u = String(u); return XO.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    if ((this.__u || '').includes('timeline/_rpc/pc')) this.addEventListener('load', () => { try { keep(this.__u, this.responseText, this.status); } catch (e) {} });
    return XS.apply(this, arguments);
  };
  return 'installed';
})()"""

NAV_JS = r"""(() => { if (!window.__pcHook) return 'nohook';
  history.pushState(null, '', %s); window.dispatchEvent(new PopStateEvent('popstate', {state: null})); return 'ok'; })()"""

READ_JS = r"""(() => { if (!window.__pcHook) return {hook: false}; const k = %s;
  const t = (window.__pc || {})[k], s = (window.__pcs || {})[k]; if (t) { delete window.__pc[k]; delete window.__pcs[k]; }
  return {hook: true, t: t || null, s: t ? (s || null) : null}; })()"""

NEUTRAL_PATH = "/maps/@0,0,2z"


@dataclass
class ReviewPageResult:
    status: str
    record: list | None = None
    raw: str = field(default="", repr=False)
    note: str | None = None
    #: the HTTP status the page's own request got (from the hook; None when not seen)
    http_status: int | None = None


def _path(url: str) -> str:
    s = urlsplit(url)
    return s.path + ("?" + s.query if s.query else "")


def find_review_record(text: str, review_id: str) -> tuple[list | None, bool]:
    """(record, readable) -- the record of ``review_id`` in a ``pc`` answer."""
    body = text[text.index("\n") + 1:] if text.startswith(")]}'") else text
    try:
        data = json.loads(body)
    except ValueError:
        return None, False
    stack = [data]
    while stack:
        x = stack.pop()
        if isinstance(x, list):
            if (len(x) > 3 and x[0] == review_id and isinstance(x[1], list) and len(x[1]) > 3
                    and isinstance(x[1][2], int)):
                return x, True
            stack.extend(v for v in x if isinstance(v, list))
    return None, True


def record_place_matches(record: list, fid: str) -> bool:
    """``record[1][0]`` is ``0x0:<cid>``; the cid half of the place's fid must match."""
    owner = record[1][0] if isinstance(record[1], list) and record[1] else None
    if not isinstance(owner, str) or ":" not in owner or ":" not in fid:
        return True  # no place reference to compare: do not reject
    return owner.split(":", 1)[1].lower() == fid.split(":", 1)[1].lower()


class ReviewPageReader:
    """Reads single-review records in one tab of the worker's session.

    The tab outlives a reader (one reader per date task), so the count of in-app navigations is
    kept on the browser with the tab and reset only when the page is really opened again."""

    #: reload the page after this many in-app navigations (history and memory grow per review)
    recycle = 200

    def __init__(self, maps: Any):
        self.maps = maps

    @property
    def browser(self):
        return self.maps.browser

    @property
    def navs(self) -> int:
        return int(getattr(self.browser, "review_page_navs", 0) or 0)

    @navs.setter
    def navs(self, value: int) -> None:
        self.browser.review_page_navs = value

    def _run(self, cmds: list[dict], timeout: float = 90.0):
        return self.browser.execute(cmds, timeout=timeout)

    def _open(self, url: str) -> None:
        """Open a review page and install the hook.  The answer for this first page came before the
        hook, so the page is moved away and back in-app to have it requested again."""
        self._run([command("open_url", url=url), command("sleep", wait_time=1.0),
                   command("run_js", js_code=HOOK_JS, out_type="str"),
                   command("run_js", js_code=NAV_JS % json.dumps(NEUTRAL_PATH), out_type="str"),
                   command("sleep", wait_time=1.0)], timeout=120)
        self.browser.current_url = None  # the Search lister reopens its google.com tab
        self.browser.review_page_ready = True
        self.navs = 0

    def _reads(self, review_id: str, plan: list[float], nav_to: str | None) -> tuple[str | None, bool]:
        """Optionally navigate, then read after each sleep in ``plan``; (text, hook alive).
        The HTTP status of the answer read is left in ``self.last_status``."""
        read = command("run_js", js_code=READ_JS % json.dumps(review_id), out_type="dict")
        cmds: list[dict] = []
        if nav_to is not None:
            cmds.append(command("run_js", js_code=NAV_JS % json.dumps(nav_to), out_type="str"))
        for wait in plan:
            cmds += [command("sleep", wait_time=wait), read]
        res = self._run(cmds, timeout=60 + sum(plan))
        alive = True
        for st in res.steps:
            if st.command != "run_js" or not st.success:
                continue
            out = (st.data or {}).get("result")
            if out == "nohook" or (isinstance(out, dict) and out.get("hook") is False):
                alive = False
            if isinstance(out, dict) and out.get("t"):
                self.last_status = out.get("s") if isinstance(out.get("s"), int) else None
                return out["t"], True
        return None, alive

    def fetch(self, fid: str, review_id: str, url: str) -> ReviewPageResult:
        result = self._fetch(fid, review_id, url)
        if result.raw:
            result.http_status = self.last_status
        return result

    last_status: int | None = None

    def _fetch(self, fid: str, review_id: str, url: str) -> ReviewPageResult:
        self.last_status = None
        if not url:
            return ReviewPageResult("not_found", note="no review URL")
        if not getattr(self.browser, "review_page_ready", False) or self.navs >= self.recycle:
            self._open(url)
        text, alive = self._reads(review_id, [1.0, 0.8, 1.0], _path(url))
        self.navs += 1
        if text is None and not alive:
            # the app replaced the document (hook gone): reopen and try this review once more
            self._open(url)
            text, alive = self._reads(review_id, [1.0, 0.8, 1.0], _path(url))
            self.navs += 1
        if text is None and alive:
            text, alive = self._reads(review_id, [0.6] * 6, None)
        if text is None:
            # one in-app retry: away and back, the page requests the review again
            self._reads(review_id, [0.5], NEUTRAL_PATH)
            text, alive = self._reads(review_id, [1.0, 1.0, 1.5], _path(url))
            self.navs += 2
        if text is None:
            return ReviewPageResult("no_answer", note="no answer within the wait after one retry")
        record, readable = find_review_record(text, review_id)
        if not readable:
            if re.search(r"<title>Error 5\d\d", text[:2000]):
                # Google's own "Error 500 (Server Error)" page: transient, read again later
                return ReviewPageResult("server_error", raw=text, note="Google answered with a server error page")
            return ReviewPageResult("unreadable", raw=text, note="answer is not JSON")
        if record is None:
            return ReviewPageResult("not_found", raw=text, note="answer without this review's record")
        if not record_place_matches(record, fid):
            return ReviewPageResult("not_found", raw=text, note=f"record of another place: {record[1][0]}")
        return ReviewPageResult("ok", record=record, raw=text)
