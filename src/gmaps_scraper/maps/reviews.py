"""Reviews: record parser and the page-driven pager.

Why page-driven: the reviews RPC (``qv9Egd``) carries an ``X-maps-bgkey``
header that the page computes for each request; a replayed request, or one whose
body was edited after the page built it (2026-10-01: only the cursor swapped for
another valid cursor of the same session), is answered with an empty payload
(``[null,...,true,[true]]``).  So the scraper lets the Maps page make its own
requests -- it selects the sort in the UI, scrolls the reviews pane and reads
the responses with a hook -- and never edits, replays or signs a request.  Each
response still is the structured RPC payload, not DOM text.

What that rejection does *not* show is that a cursor is bound to its session:
the edited request also no longer matched its header, so the cause is not
isolated.  The cursor itself is a position key (the last review's inverted
edit time in ms, a 16-byte id, the count read so far) and was byte-identical
across sessions at the same position (2026-10-04: 525 of 525 pages of two
parallel sessions).  No route of the Maps client continues a list in a fresh
document, but Google Search's reviews dialog pages the same list with an
unsigned GET that takes this key: a fresh session continued ROW NYC from saved
cursors exactly.  This pager still
reads a place within one browser session.

Selectors avoid localized labels: the sort button and the review-search
controls are found by their ``jslog`` visual-element ids, the sort options by
``role=menuitemradio`` and ``data-index`` (0 relevant, 1 newest, 2 highest,
3 lowest); the sort button is clicked with a trusted ``click_at`` at its
measured position (a locator click scrolls the pane and misses).
"""

from __future__ import annotations

import json
import logging
import re
import html
import time
import urllib.parse as up
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from ..bro.transport import command
from .client import MapsClient
from .xssi import ResponseFormatError, dig, parse_batchexecute

log = logging.getLogger(__name__)

PARSER_VERSION = "reviews-2026.10.10"

#: slack (seconds) below the earliest request still to be read when asking bro for HAR entries
HAR_SLACK = 5.0
#: how far before the last page handed out the control read goes back (seconds).  Responses
#: that bro listed late started seconds before the read; reading the card's whole HAR instead
#: (2-3 h of the tab's traffic at depth) kept a bro command busy for over 15 min (2026-10-04)
CONTROL_WINDOW = 300.0
#: HAR dumps attempted before a failed dump ends the round with an error
DUMP_ATTEMPTS = 3
FID_RE = re.compile(r"0x[0-9a-f]+:0x[0-9a-f]+")


def _epoch(stamp: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _ms(value: Any) -> float | None:
    return round(float(value), 1) if isinstance(value, (int, float)) else None


def _body_fid(body: str | None) -> str | None:
    """The place id a reviews request asks for, when the body names one."""
    if not body:
        return None
    m = FID_RE.search(up.unquote(body))
    return m.group(0) if m else None


SORT_INDEX = {"mostRelevant": 0, "newest": 1, "highestRanking": 2, "lowestRanking": 3}
#: value of the request's last element for each UI sort (verified: relevant 1, newest 2)
SORT_CODE = {"mostRelevant": 1, "newest": 2, "highestRanking": 3, "lowestRanking": 4}

JSLOG_SORT_BUTTON = "59550"
JSLOG_REVIEWS_TAB = "145620"
JSLOG_SIGNIN_DIALOG = "12877"
JSLOG_SEARCH_BUTTON = "87763"
JSLOG_SEARCH_INPUT = "61146"

REVIEWS_PER_RESPONSE = 10


def _iso_from_micros(value: Any) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1_000_000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except (OverflowError, ValueError, OSError):
        return None


def _option_label(option: Any) -> str | None:
    """A guided-question option: ``[["E:CODE"], "Label", …]`` (label at [1])."""
    label = dig(option, 1)
    if isinstance(label, str):
        return label
    label = dig(option, 0, 1)
    return label if isinstance(label, str) else None


def guided_questions(questions: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Guided questions -> per-aspect ratings (``reviewDetailedRating``) and visit context
    (``reviewContext``).  Maps serves them at ``[2][6]``, Search at ``[30]``, in one layout:
    ``[["CODE"], question, [[options], n], [[multi], n], ?, label, …, [score], …]``.

    Until 2026-10-06 the option label was read at ``option[0][1]`` (the code list), so
    ``reviewContext`` came out empty for every review."""
    ratings: dict[str, Any] = {}
    context: dict[str, Any] = {}
    for q in questions or []:
        if not isinstance(q, list):
            continue
        label = dig(q, 5) or dig(q, 1)
        score = dig(q, 11, 0)
        if score is not None and dig(q, 1):
            ratings[dig(q, 1)] = score
            continue
        options = [_option_label(o) for o in (dig(q, 2, 0) or []) if _option_label(o)]
        multi = [_option_label(o) for o in (dig(q, 3, 0) or []) if _option_label(o)]
        values = options or multi
        if not values:
            # Free-form guided answers are separate from option lists.
            free = dig(q, 10, 0)
            if isinstance(free, str) and free.strip():
                values = [html.unescape(re.sub(r"<br\s*/?>", "\n", free, flags=re.I))]
        if label and values:
            context[label] = values[0] if len(values) == 1 else ", ".join(values)
    return ratings, context


def _guided(record: list) -> tuple[dict[str, Any], dict[str, Any]]:
    """``[2][6]``: per-aspect ratings (``reviewDetailedRating``) and visit context."""
    return guided_questions(dig(record, 2, 6, default=[]))


def parse_review(record: list, *, personal_data: bool = True) -> dict[str, Any]:
    """One ``qv9Egd`` review record (``payload[2][i][0]``) -> Apify review fields."""
    author = dig(record, 1, 4, 5) or []
    text_block = dig(record, 2, 15) or []
    text = dig(text_block, 0, 0)
    translated = None
    original_language = dig(record, 2, 14, 0)
    translated_language = None
    # When Google shows a translation, the block carries a second variant.
    second = dig(text_block, 1, 0)
    if second and second != text:
        translated, translated_language = second, dig(record, 2, 14, 1) or None
    ratings, context = _guided(record)
    owner = dig(record, 3) or []
    owner_text = dig(owner, 14, 0, 0)
    origin = dig(record, 1, 13, 0)
    images = []
    for photo in dig(record, 2, 2, default=[]) or []:
        url = dig(photo, 1, 6, 0)
        if url:
            images.append(url)
    review: dict[str, Any] = {
        "reviewId": dig(record, 0),
        "reviewerId": dig(author, 3),
        "reviewerUrl": dig(author, 2, 0),
        "name": dig(author, 0),
        "reviewerNumberOfReviews": dig(author, 5),
        "isLocalGuide": bool(dig(author, 8, 0)),
        "reviewerPhotoUrl": dig(author, 1),
        "text": text,
        "textTranslated": translated,
        "publishAt": dig(record, 1, 6),
        "publishedAtDate": _iso_from_micros(dig(record, 1, 2)),
        "lastEditedAtDate": _iso_from_micros(dig(record, 1, 3)) if dig(record, 1, 3) != dig(record, 1, 2) else None,
        "likesCount": dig(record, 4, 6, 1, 0, 1, default=0) or 0,
        "reviewUrl": dig(record, 4, 3, 0),
        "reviewOrigin": origin,
        "stars": dig(record, 2, 0, 0),
        "rating": None,
        "responseFromOwnerDate": _iso_from_micros(dig(owner, 1)) if owner_text else None,
        "responseFromOwnerText": owner_text,
        "reviewImageUrls": images,
        "reviewContext": context,
        "reviewDetailedRating": ratings,
        "visitedIn": None,
        "originalLanguage": original_language,
        "translatedLanguage": translated_language,
    }
    if origin and origin != "Google" and review["stars"] is None:
        # partner reviews (Tripadvisor, hotels) carry a score like "5/5"
        review["rating"] = dig(record, 2, 0, 1)
    if not personal_data:
        for key in ("reviewerId", "reviewerUrl", "name", "reviewerPhotoUrl", "reviewUrl"):
            review[key] = None
    return review


@dataclass
class ReviewsPage:
    """One page of the reviews RPC as captured from the page."""
    records: list[list]
    next_cursor: str | None
    request_cursor: str | None
    request_sort: int | None
    request_query: str | None
    raw_size: int
    refused: bool = False
    #: the response text, kept for evidence (bro.evidence = all)
    raw: str = field(default="", repr=False)

    @property
    def ids(self) -> list[str]:
        return [str(dig(r, 0)) for r in self.records]


def parse_reviews_response(text: str, body: str | None = None) -> ReviewsPage:
    envelopes = [e for e in parse_batchexecute(text) if e.rpc == "qv9Egd"]
    if not envelopes:
        raise ResponseFormatError("no qv9Egd envelope in reviews response")
    env = envelopes[0]
    payload = env.payload
    cursor_sent = sort_sent = query_sent = None
    if body:
        try:
            outer = json.loads(up.parse_qs(body).get("f.req", ["[]"])[0])
            inner = json.loads(outer[0][0][1])
            cursor_sent = dig(inner, 1, 1)
            sort_sent = dig(inner, 12, 0) if isinstance(dig(inner, 12), list) else None
            query_sent = dig(inner, 0, 2) if isinstance(dig(inner, 0, 2), str) else None
        except (ValueError, IndexError, TypeError, KeyError):
            pass
    if payload is None:
        return ReviewsPage([], None, cursor_sent, sort_sent, query_sent, len(text), refused=env.error is not None,
                           raw=text)
    records = [dig(item, 0) for item in (dig(payload, 2) or []) if dig(item, 0)]
    refused = not records and dig(payload, 5) is True
    return ReviewsPage(records, dig(payload, 1) or None, cursor_sent, sort_sent, query_sent, len(text), refused=refused,
                       raw=text)


# ------------------------------------------------------------------ page JS

HOOK_JS = r"""(() => {
  if (window.__gmsHook) { return {already: true, n: window.__gmsCap.length}; }
  window.__gmsHook = true; window.__gmsCap = []; window.__gmsSeq = 0; window.__gmsInflight = 0;
  const now = () => performance.now();
  window.__gmsRender = [];
  try {
    new MutationObserver(ms => {
      for (const m of ms) for (const n of m.addedNodes) {
        if (n.nodeType === 1 && (n.hasAttribute('data-review-id') || n.querySelector('[data-review-id]'))) {
          window.__gmsRender.push(now()); return;
        }
      }
    }).observe(document.body, {childList: true, subtree: true});
  } catch (e) {}
  const XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send;
  const RT = Object.getOwnPropertyDescriptor(XMLHttpRequest.prototype, 'responseText').get;
  XMLHttpRequest.prototype.open = function (m, u) { this.__gmsU = String(u); return XO.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function (body) {
    const x = this;
    if (/rpcids=qv9Egd/.test(x.__gmsU || '')) {
      const seq = ++window.__gmsSeq, t0 = now(); window.__gmsInflight++;
      x.addEventListener('loadend', () => {
        window.__gmsInflight--;
        try { window.__gmsCap.push({seq: seq, s: x.status, body: typeof body === 'string' ? body : null, t: RT.call(x), t0: t0, t1: now()}); }
        catch (e) { window.__gmsCap.push({seq: seq, s: x.status, err: String(e), t0: t0, t1: now()}); }
      });
    }
    return XS.apply(this, arguments);
  };
  const F = window.fetch;
  if (F) {
    window.fetch = function (input, init) {
      const u = String((input && input.url) || input || '');
      const reqCopy = (input instanceof Request && /rpcids=qv9Egd/.test(u)) ? input.clone() : null;
      const p = F.apply(this, arguments);
      if (/rpcids=qv9Egd/.test(u)) {
        const seq = ++window.__gmsSeq, t0 = now(); window.__gmsInflight++;
        const b = init && init.body;
        const bodyP = typeof b === 'string' ? Promise.resolve(b)
          : (b instanceof URLSearchParams ? Promise.resolve(b.toString())
          : (b && typeof b.text === 'function' ? b.text()
          : (reqCopy ? reqCopy.text() : Promise.resolve(null))));
        Promise.all([bodyP.catch(() => null), p.then(r => r.clone().text().then(t => [r.status, t]))])
          .then(([body, st]) => { window.__gmsInflight--; window.__gmsCap.push({seq: seq, s: st[0], body: body, t: st[1], t0: t0, t1: now()}); })
          .catch(e => { window.__gmsInflight--; window.__gmsCap.push({seq: seq, s: 0, err: String(e), t0: t0, t1: now()}); });
      }
      return p;
    };
  }
  return {installed: true};
})()"""

HARVEST_JS = r"""(() => { const c = window.__gmsCap || []; window.__gmsCap = []; const n = performance.now();
  c.forEach(r => { r.h = n; }); return c; })()"""

#: the browser clock (marks the start/end of actions outside scroll rounds)
NOW_JS = r"""(() => ({now: performance.now()}))()"""

#: the captures plus the hook's in-flight count and the browser clock (end of a scroll round)
HARVEST_STATE_JS = r"""(() => { const c = window.__gmsCap || []; window.__gmsCap = []; const n = performance.now();
  c.forEach(r => { r.h = n; }); const rd = (window.__gmsRender || []).splice(0);
  return {caps: c, inflight: window.__gmsInflight || 0, now: n, render: rd}; })()"""

#: position of an element (by CSS selector) for a trusted click_at
RECT_JS = r"""(() => { const e = document.querySelector(%s); if (!e) return null;
  const r = e.getBoundingClientRect(); if (!r.width || !r.height) return null;
  return {x: r.x + r.width / 2, y: r.y + r.height / 2}; })()"""

#: the "Sign-in to get the best of Google Maps" dialog (VE 12877) and its Dismiss button (VE 315303);
#: it covers the pane on anonymous sessions after some review browsing (2026-10-01)
DISMISS_JS = r"""(() => { const d = document.querySelector('[role=dialog][jslog^="12877"]');
  if (!d || d.offsetParent === null) return 'none';
  const b = d.querySelector('button[jslog^="315303"]'); if (!b) return 'nobutton';
  b.click(); return 'dismissed'; })()"""

CENTER_JS = r"""(() => { const b = document.querySelector(%s); if (!b) return 'nobutton';
  b.scrollIntoView({block: 'center', behavior: 'instant'}); return 'ok'; })()"""

DIALOG_JS = r"""(() => { const d = document.querySelector('[role=dialog][jslog^="12877"]'); return !!(d && d.offsetParent !== null); })()"""

MENU_COUNT_JS = r"""(() => document.querySelectorAll('[role=menuitemradio]').length)()"""

PANE_STATE_JS = r"""(() => ({button: !!document.querySelector(%s), reviews: document.querySelectorAll('[data-review-id]').length}))()"""

FOCUS_JS = r"""(() => { const b = document.querySelector(%s); if (!b) return 'nobutton';
  b.scrollIntoView({block: 'center'}); b.focus(); return document.activeElement === b ? 'focused' : 'notfocused'; })()"""

PICK_SORT_JS = r"""(() => { const m = document.querySelector('[role=menuitemradio][data-index="%d"]');
  if (!m) return 'nomenu'; m.click(); return 'clicked'; })()"""

#: scroll the reviews pane to its bottom; report how many reviews are rendered
SCROLL_JS = r"""(() => {
  let prompt = 'none';
  const dlg = document.querySelector('[role=dialog][jslog^="12877"]');
  if (dlg && dlg.offsetParent !== null) { const b = dlg.querySelector('button[jslog^="315303"]'); if (b) { b.click(); prompt = 'dismissed'; } else { prompt = 'nobutton'; } }
  const revs = document.querySelectorAll('[data-review-id]');
  let pane = null;
  if (revs.length) {
    let el = revs[revs.length - 1].parentElement;
    while (el && el !== document.body) {
      const cs = getComputedStyle(el);
      if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) { pane = el; break; }
      el = el.parentElement;
    }
  }
  if (!pane) {
    const main = document.querySelector('div[role=main]') || document.body;
    const cands = Array.from(main.querySelectorAll('div')).filter(el => { const cs = getComputedStyle(el);
      return (cs.overflowY === 'auto' || cs.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 20; });
    cands.sort((a, b) => b.scrollHeight - a.scrollHeight);
    pane = cands[0] || null;
  }
  const before = pane ? Math.round(pane.scrollTop) : null;
  if (pane) pane.scrollTop = pane.scrollHeight;
  const ids = new Set(Array.from(revs).map(r => r.getAttribute('data-review-id')));
  return {rendered: ids.size, pane: !!pane, pending: (window.__gmsSeq || 0), prompt: prompt, now: performance.now(),
          before: before, after: pane ? Math.round(pane.scrollTop) : null,
          height: pane ? pane.scrollHeight : null, inflight: window.__gmsInflight || 0};
})()"""

#: drop rendered reviews except the last few, to keep a long session light
PRUNE_JS = r"""(() => {
  const tops = Array.from(document.querySelectorAll('[data-review-id]')).filter(e => !e.parentElement.closest('[data-review-id]'));
  const keep = 30; let removed = 0;
  for (let i = 0; i < tops.length - keep; i++) { tops[i].remove(); removed++; }
  const m = performance.memory || {};
  return {removed: removed, nodes: document.getElementsByTagName('*').length, now: performance.now(),
          heapMb: m.usedJSHeapSize ? Math.round(m.usedJSHeapSize / 1048576) : null};
})()"""


#: read-only state of the tab: page size, JS heap, the reviews pane's scroll position, the last
#: card relative to the pane, loading indicators, dialogs, long tasks and page errors since the
#: last read, and the review requests the page has completed (performance entries)
TAB_STATE_JS = r"""(() => {
  if (!window.__gmsObs) {
    window.__gmsObs = true; window.__gmsLong = []; window.__gmsErr = [];
    try { performance.setResourceTimingBufferSize(100000); } catch (e) {}
    try { new PerformanceObserver(l => { for (const e of l.getEntries()) window.__gmsLong.push(e.duration); })
            .observe({entryTypes: ['longtask']}); } catch (e) {}
    window.addEventListener('error', e => window.__gmsErr.push(String(e.message || e.type).slice(0, 120)));
    window.addEventListener('unhandledrejection', e => window.__gmsErr.push(String(e.reason).slice(0, 120)));
  }
  const cards = Array.from(document.querySelectorAll('[data-review-id]')).filter(e => !e.parentElement.closest('[data-review-id]'));
  let pane = null;
  if (cards.length) {
    let el = cards[cards.length - 1].parentElement;
    while (el && el !== document.body) {
      const cs = getComputedStyle(el);
      if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) { pane = el; break; }
      el = el.parentElement;
    }
  }
  const m = performance.memory || {};
  const mb = v => v ? Math.round(v / 1048576) : null;
  const last = cards.length ? cards[cards.length - 1].getBoundingClientRect() : null;
  const pr = pane ? pane.getBoundingClientRect() : null;
  const rpc = performance.getEntriesByType('resource').filter(e => /qv9Egd/.test(e.name));
  const lastEnd = rpc.length ? rpc[rpc.length - 1].responseEnd : null;
  const lastRpc = rpc.slice(-3).map(e => ({startS: Math.round(e.startTime / 1000), ms: Math.round(e.duration),
    bytes: e.transferSize, status: e.responseStatus, type: e.initiatorType, reqid: (e.name.match(/_reqid=(\d+)/) || [])[1]}));
  const longs = window.__gmsLong.splice(0), errs = window.__gmsErr.splice(0);
  const tab = document.querySelector('[role=tab][jslog^="145620"]');
  return {nodes: document.getElementsByTagName('*').length, cards: cards.length,
    heapMb: mb(m.usedJSHeapSize), heapTotalMb: mb(m.totalJSHeapSize), heapLimitMb: mb(m.jsHeapSizeLimit),
    pane: !!pane, scrollTop: pane ? Math.round(pane.scrollTop) : null,
    clientHeight: pane ? pane.clientHeight : null, scrollHeight: pane ? pane.scrollHeight : null,
    lastCardBelowPane: (last && pr) ? Math.round(last.bottom - pr.bottom) : null,
    progressbars: document.querySelectorAll('[role=progressbar]').length,
    dialogs: Array.from(document.querySelectorAll('[role=dialog]')).filter(d => d.offsetParent !== null).length,
    reviewsTabSelected: tab ? tab.getAttribute('aria-selected') === 'true' : null,
    visibility: document.visibilityState, hasFocus: document.hasFocus(),
    rpcDone: rpc.length, lastRpc: lastRpc, sinceLastRpcS: lastEnd !== null ? Math.round((performance.now() - lastEnd) / 1000) : null,
    longTasks: longs.length, longTaskMs: Math.round(longs.reduce((a, b) => a + b, 0)),
    errors: errs.slice(-5), errorCount: errs.length};
})()"""

#: scroll the reviews pane up by a screen (the stall probe then scrolls back down)
SCROLL_UP_JS = r"""(() => {
  const cards = Array.from(document.querySelectorAll('[data-review-id]'));
  let el = cards.length ? cards[cards.length - 1].parentElement : null;
  while (el && el !== document.body) {
    const cs = getComputedStyle(el);
    if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) break;
    el = el.parentElement;
  }
  if (!el || el === document.body) return 'nopane';
  el.scrollTop = Math.max(0, el.scrollTop - el.clientHeight * 2); return 'up';
})()"""

#: give the reviews pane keyboard focus (for End / PageDown in the stall probe)
FOCUS_PANE_JS = SCROLL_UP_JS.replace("el.scrollTop = Math.max(0, el.scrollTop - el.clientHeight * 2); return 'up';",
                                     "if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '-1'); el.focus(); "
                                     "return document.activeElement === el ? 'focused' : 'notfocused';")

#: a point inside the reviews list of the left pane (window 1280x1024, measured on 2026-10-02)
PANE_X, PANE_Y = 275.0, 700.0
WHEEL_CLICKS = -15


LIMITED_VIEW_REASON = "Google shows this anonymous session a limited view of Maps (no reviews)"


class ReviewsUnavailable(Exception):
    """The reviews pane could not be driven (no pane, no sort control)."""


class ReviewsGated(ReviewsUnavailable):
    """Google answered a sort / keyword request with its sign-in dialog (anonymous session)."""


@dataclass
class PagerState:
    pages: int = 0
    reviews_seen: int = 0
    duplicates: int = 0
    empty_scrolls: int = 0
    refused: int = 0
    signin_prompts: int = 0
    #: stalls that the in-tab probe ended (paging resumed), by the step that resumed it
    stall_resumes: list = field(default_factory=list)
    #: pages found only by the control read before giving up (missed by the regular reads)
    late_pages: int = 0
    end_reached: bool = False
    stop_reason: str | None = None
    cursor: str | None = None
    cursors_seen: set = field(default_factory=set)


class ReviewsPager:
    """Drive one place's reviews pane and yield :class:`ReviewsPage` objects.

    The pane is opened with the reviews tab selected, the XHR hook is
    installed, then the requested sort is chosen in the UI (which issues the
    first-page request through the page itself).  Every later page comes
    from scrolling the pane.  Cursor repeats and pages without new ids are
    detected; ``end_reached`` is set only when Google returns no next cursor.
    """

    def __init__(self, client: MapsClient, fid: str, *, sort: str = "newest", query: str | None = None,
                 scrolls_per_command: int = 8, scroll_wait: float = 1.6, expected: int | None = None):
        self.client = client
        self.expected = expected
        self.fid = fid
        self.sort = sort
        self.query = (query or "").strip() or None
        self.scrolls_per_command = scrolls_per_command
        self.scroll_wait = scroll_wait
        self.state = PagerState()
        #: called with one dict per review response and per pager event (diagnostics)
        self.diag = None
        #: start of the current place card (local clock, minus slack): no response of it is older
        self._card_since = time.time()
        #: HAR lower bound for the next windowed dump; it stays at or below the earliest request
        #: whose response is not read yet (bro's ``since`` filters by request start)
        self._har_since = self._card_since
        #: HAR entries whose response was read (parsed, or proved to belong elsewhere)
        self._har_done: set[str] = set()
        self._har_bad: dict[str, int] = {}
        self._dumps = 0
        #: local time when the last page was handed out (bounds the control read)
        self._last_page_at: float | None = None
        #: HAR entries seen without a usable response yet: key -> request start
        self._pending: dict[str, float] = {}
        #: pages handed out when the read part was last pruned (see :meth:`prune`)
        self._pruned_at = 0
        #: run the in-tab probe before giving up on a stalled list (:meth:`probe_stall`)
        self.stall_probe = True
        #: set by the caller to end paging once the pages already received are handed out
        self.stopping = False
        #: how a scroll round waits for the next page: "rounds" (A: a fixed batch of scrolls and
        #: sleeps, then one read) or "await" (B: one scroll, then short reads; wait while the
        #: page's request is in flight, scroll again when none is; bounded by await_budget)
        self.wait_mode = "rounds"
        self.await_budget = 30.0
        #: ABBA comparison of the two modes: blocks of this many pages (0 = off), from this page
        #: on, each block also ends after ab_block_minutes; a switch happens only after a round
        #: that ended with no review request in flight
        self.ab_block = 0
        self.ab_from = 200
        self.ab_block_minutes = 60.0
        self._ab_index = -1
        self._ab_start: tuple[int, float] | None = None
        #: the hook's in-flight count at the end of the last round (None = not known)
        self._inflight: int | None = None
        #: pages captured while verifying the chosen order, handed to pages() first
        self._prefetched: list[ReviewsPage] = []

    # ------------------------------------------------------------ helpers
    def _js(self, code: str, out_type: str | None = "dict", timeout: float = 60.0) -> Any:
        return self.client.js(code, out_type=out_type, timeout=timeout)

    def _rect(self, selector: str) -> dict | None:
        return self._js(RECT_JS % json.dumps(selector), "dict")

    def _click_selector(self, selector: str, settle: float = 1.2) -> bool:
        pos = self._rect(selector)
        if not pos:
            return False
        self.client.browser.execute([command("click_at", x=float(pos["x"]), y=float(pos["y"])),
                                     command("sleep", wait_time=settle)], timeout=60)
        return True

    # -------------------------------------------------------------- setup
    def limited_view(self) -> bool:
        """Google's "You're seeing a limited view of Google Maps" banner (seen 2026-10-02 on anonymous
        sessions, varying by session): the pane then serves no reviews."""
        return bool(self._js("(() => /limited view of Google Maps/i.test(document.body ? document.body.innerText : ''))()",
                             "bool"))

    def _warm_up(self) -> None:
        """Visit google.com and a Maps search once per session before any place page (the same
        warm-up as the native driver, so the two differ only in how the pane is driven)."""
        browser = self.client.browser
        if getattr(browser, "native_warm", False):
            return
        hl, gl = self.client.hl, self.client.gl
        search = f"https://www.google.com/maps/search/restaurants/?hl={hl}&gl={gl}"
        browser.execute([command("open_url", url=f"https://www.google.com/?hl={hl}&gl={gl}"),
                         command("sleep", wait_time=3.0), command("open_url", url=search),
                         command("sleep", wait_time=5.0)], timeout=150)
        browser.native_warm = True
        browser.current_url = search

    def open(self) -> None:
        """Open the place, hook XHRs, switch to the reviews tab, then apply sort / keyword.

        The overview is opened first so the hook exists before the reviews tab is
        selected: the review list is only requested once the pane is scrolled or a
        sort is chosen, so even the first page goes through the hook.  Choosing a
        sort or a keyword can be refused to anonymous sessions with a sign-in
        dialog; that is reported as :class:`ReviewsGated`.
        """
        from . import pb
        self._warm_up()
        self.client.ensure_maps()
        self._card_since = self._har_since = time.time() - HAR_SLACK
        self.client.open(pb.place_page_url(self.fid, hl=self.client.hl, gl=self.client.gl), settle=2.0)
        self._js(HOOK_JS, "dict")
        self._dismiss()
        tab = f'[role=tab][jslog^="{JSLOG_REVIEWS_TAB}"]'
        for attempt in range(3):
            pos = self._rect(tab)
            if not pos:
                if attempt == 2:
                    if self.limited_view():
                        raise ReviewsGated(LIMITED_VIEW_REASON)
                    raise ReviewsUnavailable("the place page has no reviews tab")
                time.sleep(2.0)
                continue
            self.client.browser.execute([command("click_at", x=float(pos["x"]), y=float(pos["y"])),
                                         command("sleep", wait_time=1.5)], timeout=60)
            selected = self._js("(() => { const t = document.querySelector(%s); return !!t && t.getAttribute('aria-selected') === 'true'; })()"
                                % json.dumps(tab), "bool")
            if selected:
                break
            self._dismiss()
        else:
            raise ReviewsUnavailable("the reviews tab could not be selected")
        if self.sort != "mostRelevant":
            self._choose_sort(SORT_INDEX[self.sort])
        if self.query:
            self._apply_query()
        if self.sort != "mostRelevant" or self.query:
            self._verify_order()

    def _verify_order(self, rounds: int = 2) -> None:
        """The chosen order/keyword must produce a page of that order.

        On some anonymous sessions the menu opens and the item can be clicked, yet
        no request of the chosen order follows (2026-10-01, NL sessions); the same
        sessions page Google's default order normally.  That is treated like the
        sign-in dialog: :class:`ReviewsGated`.
        """
        for _ in range(rounds):
            pages = self.harvest() or self.scroll_round(scrolls=3)
            if pages:
                self._prefetched = pages
                return
            if self._js(DIALOG_JS, "bool"):
                self._dismiss()
                raise ReviewsGated("the chosen review order is answered with Google's sign-in dialog")
        raise ReviewsGated("no page of the chosen review order arrived after choosing it")

    def _menu_items(self) -> int:
        return int(self._js("(() => document.querySelectorAll('[role=menuitemradio]').length)()", "int") or 0)

    def _dismiss(self) -> None:
        if self._js(DISMISS_JS, "str") == "dismissed":
            self.state.signin_prompts += 1
            time.sleep(1.0)

    def _open_menu(self, selector: str) -> str:
        """Open a pane menu: trusted click at the settled button position, else focus + Enter.

        A trusted click also gives the window keyboard focus; on a fresh session
        a bare Enter key did not reach the page (2026-10-01)."""
        sel = json.dumps(selector)
        self._dismiss()
        res = self.client.browser.execute([
            command("run_js", js_code=CENTER_JS % sel, out_type="str"), command("sleep", wait_time=0.8),
            command("run_js", js_code=RECT_JS % sel, out_type="dict")], timeout=60)
        pos = (res.steps[-1].data or {}).get("result") if res.steps and res.steps[-1].success else None
        if not pos:
            return "nobutton"
        res = self.client.browser.execute([
            command("click_at", x=float(pos["x"]), y=float(pos["y"])), command("sleep", wait_time=1.0),
            command("run_js", js_code=MENU_COUNT_JS, out_type="int"),
            command("run_js", js_code=DIALOG_JS, out_type="bool")], timeout=60)
        if int((res.steps[-2].data or {}).get("result") or 0) > 0:
            return "click"
        if (res.steps[-1].data or {}).get("result"):
            self._dismiss()
            return "signin"
        res = self.client.browser.execute([
            command("run_js", js_code=FOCUS_JS % sel, out_type="str"), command("press", keys="enter"),
            command("sleep", wait_time=1.0), command("run_js", js_code=MENU_COUNT_JS, out_type="int")], timeout=60)
        if int((res.steps[-1].data or {}).get("result") or 0) > 0:
            return "key"
        return "closed"

    def _wait_for_pane(self, timeout: float = 12.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self._js(PANE_STATE_JS % json.dumps(f'button[jslog^="{JSLOG_SORT_BUTTON}"]'), "dict") or {}
            if state.get("button"):
                return True
            time.sleep(1.5)
        return False

    def _choose_sort(self, index: int, attempts: int = 4) -> None:
        if not self._wait_for_pane():
            raise ReviewsUnavailable("reviews pane did not render a sort control")
        last = None
        signins = 0
        for attempt in range(attempts):
            how = self._open_menu(f'button[jslog^="{JSLOG_SORT_BUTTON}"]')
            if how == "signin":
                signins += 1
                if signins >= 2:
                    raise ReviewsGated("choosing a review order is answered with Google's sign-in dialog")
                time.sleep(1.5)
                continue
            if how in ("click", "key"):
                picked = self._js(PICK_SORT_JS % index, "str")
                if picked == "clicked":
                    time.sleep(1.0)
                    return
                last = f"menu via {how}, pick={picked}"
            else:
                last = how
            time.sleep(1.5 + attempt)
        raise ReviewsUnavailable(f"sort menu did not open ({last})")

    def _apply_query(self) -> None:
        button = f'button[jslog^="{JSLOG_SEARCH_BUTTON}"]'
        field = f'input[jslog^="{JSLOG_SEARCH_INPUT}"]'
        visible = ("(() => { const i = document.querySelector(%s); return !!(i && i.offsetParent !== null); })()"
                   % json.dumps(field))
        pos = None
        for attempt in range(3):
            if not self._js(visible, "bool"):
                sel = json.dumps(button)
                res = self.client.browser.execute([
                    command("run_js", js_code=CENTER_JS % sel, out_type="str"), command("sleep", wait_time=0.8),
                    command("run_js", js_code=RECT_JS % sel, out_type="dict")], timeout=60)
                bpos = (res.steps[-1].data or {}).get("result") if res.steps and res.steps[-1].success else None
                if not bpos:
                    raise ReviewsUnavailable("no review-search control")
                res = self.client.browser.execute([command("click_at", x=float(bpos["x"]), y=float(bpos["y"])),
                                                   command("sleep", wait_time=1.0),
                                                   command("run_js", js_code=DIALOG_JS, out_type="bool")], timeout=60)
                if (res.steps[-1].data or {}).get("result"):
                    self._dismiss()
                    if attempt >= 1:
                        raise ReviewsGated("searching reviews is answered with Google's sign-in dialog")
            pos = self._rect(field)
            if pos:
                break
            time.sleep(1.0 + attempt)
        if not pos:
            raise ReviewsUnavailable("no review-search input")
        # a locator "type" left the field empty; a trusted click-and-type at the field works
        res = self.client.browser.execute([
            command("type_at", x=float(pos["x"]), y=float(pos["y"]), text=self.query),
            command("press", keys="enter"), command("sleep", wait_time=2.0),
            command("run_js", js_code="(() => { const i = document.querySelector(%s); return i ? i.value : null; })()"
                    % json.dumps(field), out_type="str")], timeout=90)
        typed = (res.steps[-1].data or {}).get("result") if res.steps and res.steps[-1].success else None
        if typed != self.query:
            raise ReviewsUnavailable(f"review search text not accepted (field holds {typed!r})")

    # ------------------------------------------------------------- paging
    def _note(self, **rec: Any) -> None:
        if self.diag:
            try:
                block = {"block": self._ab_index} if self._ab_start is not None else {}
                self.diag({"fid": self.fid, "sort": self.sort, "at": time.time(), **block, **rec})
            except Exception:  # noqa: BLE001 -- diagnostics never stop the pager
                log.debug("diagnostics callback failed", exc_info=True)

    def _note_round(self, res, wall: float, parse: float, pages: int) -> None:
        """Where the time of one scroll round goes: bro step durations per command, the rest of
        the command's wall time (queue, delivery), and local parsing of the dump."""
        by: dict[str, float] = {}
        for st in res.steps:
            by[st.command] = by.get(st.command, 0.0) + float(st.duration or 0.0)
        dump = res.steps[-1] if res.steps else None
        self._note(event="round", mode=self.wait_mode, wall=round(wall, 2), steps={k: round(v, 2) for k, v in by.items()},
                   overhead=round(wall - sum(by.values()), 2), parse=round(parse, 3), pages=pages,
                   dumpBytes=len(json.dumps(dump.data)) if dump is not None and dump.data is not None else 0,
                   readPages=self.state.pages)

    def harvest(self) -> list[ReviewsPage]:
        return self._hook_pages(self._js(HARVEST_JS, "list") or [])

    def _hook_pages(self, rows: list) -> list[ReviewsPage]:
        """Pages from the hook's captures; every capture is recorded, also the ones dropped
        (failed, empty, unreadable, another order, refused), so an unexplained request can be
        identified afterwards."""
        pages = []
        for row in sorted(rows or [], key=lambda r: r.get("seq", 0)):
            rec = {"event": "rpc", "via": "hook", "seq": row.get("seq"), "status": row.get("s"),
                   "size": len(row.get("t") or ""), "mode": self.wait_mode,
                   # browser clock (performance.now(), ms): request start, response end, read
                   "t0": _ms(row.get("t0")), "t1": _ms(row.get("t1")), "h": _ms(row.get("h"))}
            if row.get("err") or not row.get("t"):
                self._note(**rec, outcome="failed" if row.get("err") else "empty", error=row.get("err"))
                continue
            try:
                page = parse_reviews_response(row["t"], row.get("body"))
            except ResponseFormatError as exc:
                log.warning("[%s] unreadable reviews response: %s", self.fid, exc)
                self._note(**rec, outcome="unreadable", error=str(exc)[:200])
                continue
            wanted = self._wanted(page)
            self._note(**rec, outcome="page" if wanted else "other_order", requestCursor=page.request_cursor,
                       nextCursor=page.next_cursor, records=len(page.records), refused=page.refused,
                       requestSort=page.request_sort)
            if wanted:
                pages.append(page)
        return pages

    def _wanted(self, page: ReviewsPage) -> bool:
        """Only pages of the requested order and keyword belong to this run."""
        if page.request_sort is not None and page.request_sort != SORT_CODE[self.sort]:
            return False
        if (page.request_query or None) != self.query:
            return False
        return True

    def scroll_round(self, scrolls: int | None = None) -> list[ReviewsPage]:
        if self.wait_mode == "await" and scrolls is None:
            return self._await_round()
        cmds = []
        for _ in range(scrolls or self.scrolls_per_command):
            cmds += [command("run_js", js_code=SCROLL_JS, out_type="dict"), command("sleep", wait_time=self.scroll_wait)]
        cmds.append(command("run_js", js_code=HARVEST_STATE_JS, out_type="dict"))
        t0 = time.time()
        res = self.client.browser.execute(cmds, timeout=60 + self.scrolls_per_command * (self.scroll_wait + 8))
        t1 = time.time()
        rows = self._round_rows(res)
        pages = self._hook_pages(rows)
        self._note_round(res, t1 - t0, time.time() - t1, len(pages))
        return pages

    def _round_rows(self, res) -> list:
        """Captures of a round's last step (a list, or HARVEST_STATE_JS's dict); records the
        round's scrolls and the in-flight count."""
        scrolls = []
        for step in res.steps[:-1]:
            if step.command != "run_js" or not step.success:
                continue
            out = (step.data or {}).get("result") or {}
            if not isinstance(out, dict):
                continue
            if out.get("prompt") == "dismissed":
                self.state.signin_prompts += 1
            if "now" in out:
                scrolls.append([_ms(out.get("now")), out.get("before"), out.get("after"), out.get("height"),
                                out.get("inflight")])
        last = (res.steps[-1].data or {}).get("result") if res.steps and res.steps[-1].success else None
        render = []
        if isinstance(last, dict):
            rows, self._inflight = last.get("caps") or [], last.get("inflight")
            end = _ms(last.get("now"))
            render = [_ms(t) for t in last.get("render") or []]
        else:
            rows, self._inflight, end = last or [], None, None
        if scrolls or end is not None:
            # browser clock (ms); each scroll: [time, scrollTop before, after, scrollHeight, in flight];
            # render: when review cards were added to the page
            self._note(event="scrolls", mode=self.wait_mode, scrolls=scrolls, end=end, inflight=self._inflight,
                       render=render, readPages=self.state.pages)
        return rows

    def _await_round(self) -> list[ReviewsPage]:
        """Mode B: scroll once, then read after a short sleep; while the page's request is in
        flight only wait and read again, when none is (and nothing arrived) scroll again.  Ends
        on the first page or after ``await_budget`` seconds (an empty round, as in mode A)."""
        trigger = [command("run_js", js_code=SCROLL_JS, out_type="dict"), command("sleep", wait_time=1.0)]
        read = command("run_js", js_code=HARVEST_STATE_JS, out_type="dict")
        cmds = trigger + [read]
        started = time.time()
        batches, waits, triggers, wall, parse = 0, 0, 1, 0.0, 0.0
        steps: dict[str, float] = {}
        pages: list[ReviewsPage] = []
        while True:
            t0 = time.time()
            res = self.client.browser.execute(cmds, timeout=90)
            t1 = time.time()
            batches += 1
            wall += t1 - t0
            for st in res.steps:
                steps[st.command] = steps.get(st.command, 0.0) + float(st.duration or 0.0)
            pages = self._hook_pages(self._round_rows(res))
            parse += time.time() - t1
            if pages or time.time() - started >= self.await_budget:
                break
            if self._inflight:
                cmds, waits = [command("sleep", wait_time=1.5), read], waits + 1
            else:
                cmds, triggers = trigger + [read], triggers + 1
        self._note(event="round", mode="await", wall=round(wall, 2), steps={k: round(v, 2) for k, v in steps.items()},
                   overhead=round(wall - sum(steps.values()), 2), parse=round(parse, 3), pages=len(pages),
                   batches=batches, waits=waits, triggers=triggers, readPages=self.state.pages)
        return pages

    def _ab_switch(self) -> None:
        """ABBA blocks of the two wait modes (see ``ab_block``); called between rounds."""
        if not self.ab_block or self.state.pages < self.ab_from:
            return
        if self._ab_start is not None:
            pages0, t0 = self._ab_start
            due = (self.state.pages - pages0 >= self.ab_block
                   or time.time() - t0 >= self.ab_block_minutes * 60)
            if not due or self._inflight:  # unknown (None) counts as idle
                return
            self._note(event="ab_block", index=self._ab_index, mode=self.wait_mode, fromPage=pages0,
                       toPage=self.state.pages, minutes=round((time.time() - t0) / 60, 2), ended=True,
                       commands=self._commands())
        self._ab_index += 1
        self.wait_mode = "rounds" if "ABBA"[self._ab_index % 4] == "A" else "await"
        self._ab_start = (self.state.pages, time.time())
        self._note(event="ab_block", index=self._ab_index, mode=self.wait_mode, fromPage=self.state.pages,
                   commands=self._commands())

    def _commands(self) -> int | None:
        return getattr(self.client.browser, "commands_sent", None)

    def _run(self, cmds: list[dict], timeout: float = 90.0):
        return self.client.browser.execute(cmds, timeout=timeout)

    def _har_pages(self, data: Any, *, since: float | None = None, control: bool = False) -> list[ReviewsPage]:
        """Pages of this place in a HAR dump that were not read before.

        An entry is marked read only once its response parsed (or proved to belong to another
        place); an entry without a body yet, or with an unreadable one, is read again on a later
        dump, and the next dump's ``since`` stays at or below its start."""
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                return []
        har = (data or {}).get("har_logs") or data or {}
        entries = (har.get("log") or {}).get("entries") or []
        pages: list[tuple[str, ReviewsPage]] = []
        open_starts: list[float] = []
        last_done: float | None = None
        for e in entries:
            req = e.get("request") or {}
            url = req.get("url") or ""
            if "qv9Egd" not in url:
                continue
            body = (req.get("postData") or {}).get("text") or ""
            started = e.get("startedDateTime")
            t0 = _epoch(started)
            key = f"{started}|{url[-80:]}|{body[-120:]}"
            if key in self._har_done:
                continue
            resp = e.get("response") or {}
            text = (resp.get("content") or {}).get("text") or ""
            rec = {"event": "rpc", "started": started, "ms": e.get("time"), "status": resp.get("status"),
                   "size": len(text), "since": since, "dump": self._dumps, "control": control,
                   "place": _body_fid(body)}
            if rec["place"] and rec["place"] != self.fid:
                self._har_done.add(key)
                self._pending.pop(key, None)
                self._note(**rec, outcome="other_place")
                continue
            if not text:
                if t0 is not None:
                    open_starts.append(t0)
                    self._pending[key] = t0
                self._note(**rec, outcome="pending")
                continue
            try:
                page = parse_reviews_response(text, body or None)
            except ResponseFormatError as exc:
                n = self._har_bad[key] = self._har_bad.get(key, 0) + 1
                if n < DUMP_ATTEMPTS:
                    if t0 is not None:
                        open_starts.append(t0)
                        self._pending[key] = t0
                else:
                    self._har_done.add(key)
                    self._pending.pop(key, None)
                log.warning("[%s] unreadable reviews response in HAR (%d): %s", self.fid, n, exc)
                self._note(**rec, outcome="unreadable", error=str(exc)[:200])
                continue
            self._har_done.add(key)
            self._pending.pop(key, None)
            if t0 is not None:
                last_done = t0 if last_done is None else max(last_done, t0)
            wanted = self._wanted(page)
            self._note(**rec, outcome="page" if wanted else "other_order", requestCursor=page.request_cursor,
                       nextCursor=page.next_cursor, records=len(page.records), refused=page.refused)
            if wanted:
                pages.append((str(started or ""), page))
        if not control:
            if open_starts:
                self._har_since = max(self._card_since, min(open_starts) - HAR_SLACK)
            elif last_done is not None:
                # every later request starts after the latest one read
                self._har_since = max(self._card_since, last_done - HAR_SLACK)
        return [p for _, p in sorted(pages, key=lambda x: x[0])]

    def _dump_cmd(self, since: float) -> dict:
        return command("dump_har_logs", since=since)

    def _read_dump(self, res, since: float, control: bool = False) -> list[ReviewsPage]:
        """Pages from the batch's final dump; a failed dump is repeated, never read as empty."""
        self._dumps += 1
        step = res.steps[-1] if res.steps else None
        if step is not None and step.command == "dump_har_logs" and step.success:
            return self._har_pages(step.data, since=since, control=control)
        self._note(event="dump_failed", since=since, dump=self._dumps,
                   error=(getattr(step, "error_name", None) or "failed") if step else "no step")
        for _ in range(DUMP_ATTEMPTS - 1):
            again = self._run([self._dump_cmd(since)], timeout=180)
            st = again.steps[-1] if again.steps else None
            if st is not None and st.success:
                return self._har_pages(st.data, since=since, control=control)
        raise RuntimeError("bro dump_har_logs failed; the review responses of this round are unknown")

    def recover(self) -> list[ReviewsPage]:
        """Before giving up on a list: read the HAR log again from CONTROL_WINDOW seconds before
        the last page handed out (no moving window) and return the pages the reads missed."""
        # the last CONTROL_WINDOW seconds before the last page, and never past a request that an
        # earlier read saw still unanswered
        bound = (self._last_page_at or 0.0) - CONTROL_WINDOW
        if self._pending:
            bound = min(bound, min(self._pending.values()) - HAR_SLACK)
        since = max(self._card_since, bound)
        st = self.state
        pages = self._read_dump(self._run([self._dump_cmd(since)], timeout=300), since, control=True)
        # the script driver reads pages through its hook, not the HAR log: a control read
        # returns them again; only pages not handed out yet count as missed
        pages = [p for p in pages
                 if p.request_cursor not in st.cursors_seen and (p.request_cursor or not st.pages)]
        st.late_pages += len(pages)
        self._note(event="control_read", since=since, late=len(pages))
        if pages:
            log.warning("[%s] control HAR read found %d page(s) missed by windowed reads", self.fid, len(pages))
        return pages

    #: prune the read part of the list every this many pages (0 = never)
    prune_every = 5

    def prune(self) -> None:
        """Remove reviews already read from the page.

        Without it the pane grows with every page: 10 s per page at the start, 25 s after 70
        pages, then no new pages at 1 220 reviews (ROW NYC, 2026-10-02).  This is the only
        script on these sessions; it changes no request and runs after the order is chosen."""
        # by pages since the last successful prune: a round can bring several pages (4 -> 6) or
        # none (still 5), so "pages divisible by N" would skip some prunes and repeat others
        if not self.prune_every or self.state.pages - self._pruned_at < self.prune_every:
            return
        try:
            res = self._run([command("run_js", js_code=PRUNE_JS, out_type="dict", timeout=30)], timeout=60)
        except RuntimeError:
            return
        step = res.steps[-1] if res.steps else None
        if step is None or not step.success:
            return
        self._pruned_at = self.state.pages
        out = (step.data or {}).get("result") or {}
        if isinstance(out, dict):
            # page size and JS heap at this depth: what grows while paging slows down
            self._note(event="prune", readPages=self.state.pages, **out)
            if out.get("now") is not None:
                self._note(event="action", kind="prune", t0=_ms(out["now"]), t1=_ms(out["now"]),
                           readPages=self.state.pages)

    #: record the tab state every this many pages (and on every empty round)
    telemetry_every = 50
    #: after a round that brought no page, scroll the pane up a little and back down before the
    #: next one.  Measured on ROW NYC (2026-10-04, page ~550): with the pane already at its
    #: bottom, further wheel-down rounds and a 30 s wait brought nothing, a wheel up/down did
    #: (3 of 3); the page asks for the next reviews on a scroll it can see
    bounce_after_empty = False

    def _act(self, kind: str, cmds: list[dict], timeout: float = 120.0):
        """Run an action outside the scroll rounds (probe step, bounce) between two browser-clock
        marks, so the timing reports see every input the page got, not only round scrolls."""
        mark = command("run_js", js_code=NOW_JS, out_type="dict")
        res = self._run([mark, *cmds, mark], timeout=timeout)
        steps = res.steps or []

        def at(i):
            st = steps[i] if -len(steps) <= i < len(steps) else None
            v = (st.data or {}).get("result") if st is not None and st.success else None
            return _ms(v.get("now")) if isinstance(v, dict) else None
        self._note(event="action", kind=kind, t0=at(0), t1=at(-1) if len(steps) == len(cmds) + 2 else None,
                   readPages=self.state.pages)
        return res

    def bounce(self) -> None:
        try:
            self._act("bounce", [command("scroll", clicks=5, x=PANE_X, y=PANE_Y), command("sleep", wait_time=1.0),
                                 command("scroll", clicks=WHEEL_CLICKS, x=PANE_X, y=PANE_Y)], timeout=60)
        except RuntimeError as exc:
            self._note(event="bounce", error=str(exc)[:200])

    def tab_state(self, why: str) -> dict:
        """Record the tab's state (read-only; see TAB_STATE_JS) in the diagnostics."""
        try:
            res = self._run([command("run_js", js_code=TAB_STATE_JS, out_type="dict", timeout=30)], timeout=60)
        except RuntimeError as exc:
            self._note(event="tab_state", why=why, error=str(exc)[:200])
            return {}
        step = res.steps[-1] if res.steps else None
        out = ((step.data or {}).get("result") if step is not None and step.success else None) or {}
        if not isinstance(out, dict):
            out = {}
        self._note(event="tab_state", why=why, readPages=self.state.pages, **out)
        return out

    def _probe_read(self) -> list[ReviewsPage]:
        """New pages after a probe step: the driver's own read, then the whole HAR of the card."""
        st = self.state
        pages = [p for p in self.harvest() if p.request_cursor not in st.cursors_seen]
        return pages or self.recover()

    def probe_stall(self) -> list[ReviewsPage]:
        """The list stopped growing although Google gave a next cursor: try, in the same tab and
        one at a time, what could make the page ask for the next page, and record which step
        (if any) did.  Each step is followed by a read of the driver's captures and of the full
        HAR log, so a late response is not mistaken for the effect of a later step."""
        steps = [
            ("wait_30s", [command("sleep", wait_time=30.0)]),
            ("wheel_up_down", [command("scroll", clicks=5, x=PANE_X, y=PANE_Y), command("sleep", wait_time=2.0)]
             + [command("scroll", clicks=WHEEL_CLICKS, x=PANE_X, y=PANE_Y), command("sleep", wait_time=2.0)] * 4),
            ("js_scroll_up_then_bottom", [command("run_js", js_code=SCROLL_UP_JS, out_type="str"),
                                          command("sleep", wait_time=2.0),
                                          command("run_js", js_code=SCROLL_JS, out_type="dict"),
                                          command("sleep", wait_time=4.0)]),
            ("focus_pane_end_key", [command("run_js", js_code=FOCUS_PANE_JS, out_type="str"),
                                    command("press", keys="End"), command("sleep", wait_time=4.0)]),
        ]
        self.tab_state("stall")
        for name, cmds in steps:
            try:
                self._act(f"probe:{name}", cmds, timeout=120)
            except RuntimeError as exc:
                self._note(event="stall_probe", step=name, error=str(exc)[:200])
                continue
            pages = self._probe_read()
            state = self.tab_state(f"after_{name}")
            self._note(event="stall_probe", step=name, pages=len(pages), rpcDone=state.get("rpcDone"))
            if pages:
                self.state.stall_resumes.append(name)
                log.warning("[%s] stalled list resumed after probe step %s (%d page(s))", self.fid, name, len(pages))
                return pages
        return []

    def pages(self, *, max_rounds: int = 10_000, idle_rounds: int = 3) -> Iterator[ReviewsPage]:
        """Yield pages until Google reports the end, a cursor repeats or nothing new arrives."""
        st = self.state
        queue = list(self._prefetched) + self.harvest()
        self._prefetched = []
        rounds = 0
        idle = 0
        while True:
            for page in queue:
                if page.refused:
                    st.refused += 1
                    self._note(event="dropped", why="refused", requestCursor=page.request_cursor)
                    continue
                if page.request_cursor and page.request_cursor in st.cursors_seen:
                    st.duplicates += 1
                    self._note(event="dropped", why="duplicate_cursor", requestCursor=page.request_cursor)
                    continue
                if page.request_cursor:
                    st.cursors_seen.add(page.request_cursor)
                st.pages += 1
                st.reviews_seen += len(page.records)
                st.cursor = page.next_cursor
                if (not page.next_cursor and st.pages == 1 and not page.request_cursor and self.expected
                        and len(page.records) < min(self.expected, REVIEWS_PER_RESPONSE)
                        and not self.query):
                    # a short first page without continuation while Google counts more reviews:
                    # the session is served a preview only (seen 2026-10-01: 5 of 1178)
                    raise ReviewsGated(f"only a preview of {len(page.records)} of {self.expected} reviews is served")
                self._last_page_at = time.time()
                yield page
                if not page.next_cursor:
                    st.end_reached = True
                    st.stop_reason = "source_end"
                    return
                if page.next_cursor in st.cursors_seen:
                    st.stop_reason = "cursor_cycle"
                    return
            if st.refused >= 3:
                st.stop_reason = "refused"
                return
            if self.stopping:
                st.stop_reason = "stopping"
                return
            rounds += 1
            if rounds > max_rounds:
                st.stop_reason = "round_limit"
                return
            if idle and self.bounce_after_empty:
                self.bounce()
            self._ab_switch()
            queue = self.scroll_round()
            if queue:
                idle = 0
                if self.telemetry_every and st.pages // self.telemetry_every != (st.pages - len(queue)) // self.telemetry_every:
                    self.tab_state("depth")
            else:
                idle += 1
                st.empty_scrolls += 1
                if st.pages:
                    self.tab_state("empty_round")
                if idle >= idle_rounds:
                    # pages the regular reads missed (already handed-out ones are dropped there)
                    late = self.recover()
                    if not late and st.pages and self.stall_probe and st.cursor:
                        late = self.probe_stall()
                    if late:
                        queue, idle = late, 0
                        continue
                    if st.pages == 0 and self.limited_view():
                        raise ReviewsGated(LIMITED_VIEW_REASON)
                    st.stop_reason = "no_new_pages"
                    return
            if st.pages >= 4:
                self.prune()
