"""Reviews driven by native bro input; responses read from bro's HAR log.

The pane is opened after a warm-up (google.com, then a Maps search), the Reviews tab, the sort
menu and the order are chosen with ``locate`` + ``click_at``, pages are requested by wheel
scrolling, and the review responses (``qv9Egd``) are taken from ``dump_har_logs``.  The HAR log
holds the page's requests whether or not they went through the proxy (policy ``basic`` is
enough).  The only ``run_js`` on these sessions is the periodic pruning of reviews already read
(:meth:`prune`); it sends no request.

Why not the script driver: on 2026-10-02 it got 0 reviews in 5 of 5 sessions while bro's HAR of
the same sessions held the newest pages (A/B testing).  Maps sent the
review requests with ``fetch``; that hook watched only XHR.  Those "refusals" were a blind spot
of the reader, not shown to be Google reacting to ``run_js``.  Real gates remain (the
limited-view banner, preview-only lists).  This driver does not depend on how the page sends
its requests.

The pager keeps the interface and the stop logic of :class:`ReviewsPager`; only the way the
pane is driven and read differs.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..bro.transport import command
from . import pb
from .reviews import (HAR_SLACK, PANE_X, PANE_Y, WHEEL_CLICKS, JSLOG_REVIEWS_TAB, JSLOG_SEARCH_BUTTON, JSLOG_SEARCH_INPUT, JSLOG_SORT_BUTTON,
                      LIMITED_VIEW_REASON, SORT_INDEX, ReviewsGated, ReviewsPage, ReviewsPager, ReviewsUnavailable)

log = logging.getLogger(__name__)

#: Google's consent page ("Reject all")
CONSENT_REJECT = 'button[jsname="tWT92d"]'
def _css(value: str) -> dict[str, str]:
    return {"strategy": "css", "value": value}


class NativeReviewsPager(ReviewsPager):
    """:class:`ReviewsPager` driven by native clicks/scrolls; pages come from the HAR log."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.browser = self.client.browser

    # ------------------------------------------------------------ helpers
    def _box(self, selector: str, timeout_ms: int = 6000) -> dict | None:
        """The element's bounding box (bro ``locate``), or None when it does not show up."""
        res = self._run([command("locate", strategy="css", value=selector, timeout_ms=timeout_ms)], timeout=60)
        step = res.steps[-1] if res.steps else None
        if not step or not step.success or not isinstance(step.data, dict):
            return None
        return step.data.get("bounding_box") or None

    def _click(self, selector: str, settle: float = 1.5) -> bool:
        """``locate`` + a trusted ``click_at`` on the centre.  Locator clicks (``click`` with a
        locator) scrolled the pane but did not open Google's sort menu (2026-10-02)."""
        box = self._box(selector)
        if not box:
            return False
        x = float(box["x"]) + float(box.get("width") or 0) / 2
        y = float(box["y"]) + float(box.get("height") or 0) / 2
        res = self._run([command("click_at", x=x, y=y), command("sleep", wait_time=settle)], timeout=60)
        return bool(res.steps) and res.steps[0].success

    def _text(self) -> str:
        res = self._run([command("parse_text", format="plain")], timeout=90)
        data = res.steps[-1].data if res.steps and res.steps[-1].success else ""
        if isinstance(data, dict):
            data = data.get("text") or data.get("result") or json.dumps(data, ensure_ascii=False)
        return data if isinstance(data, str) else ""

    def limited_view(self) -> bool:
        return "limited view of Google Maps" in self._text()

    def _warm_up(self) -> None:
        """Visit google.com and a Maps search once per session before any place page."""
        if getattr(self.browser, "native_warm", False):
            return
        hl, gl = self.client.hl, self.client.gl
        res = self._run([command("open_url", url=f"https://www.google.com/?hl={hl}&gl={gl}"),
                         command("sleep", wait_time=3.0), command("get_url")], timeout=120)
        url = str(((res.steps[-1].data or {}) if res.steps else {}).get("url") or "")
        if "consent." in url:
            self._click(CONSENT_REJECT, settle=3.0)
        self._run([command("open_url", url=f"https://www.google.com/maps/search/restaurants/?hl={hl}&gl={gl}"),
                   command("sleep", wait_time=5.0)], timeout=120)
        self.browser.native_warm = True
        self.browser.current_url = None

    # -------------------------------------------------------------- setup
    def open(self) -> None:
        self._warm_up()
        self._card_since = time.time() - HAR_SLACK
        self._har_since = self._card_since
        self._run([command("open_url", url=pb.place_page_url(self.fid, hl=self.client.hl, gl=self.client.gl)),
                   command("sleep", wait_time=5.0)], timeout=120)
        self.browser.current_url = None
        if not self._click(f'[role=tab][jslog^="{JSLOG_REVIEWS_TAB}"]', settle=3.0):
            if self.limited_view():
                raise ReviewsGated(LIMITED_VIEW_REASON)
            raise ReviewsUnavailable("the place page has no reviews tab")
        if self.sort != "mostRelevant":
            if not self._click(f'button[jslog^="{JSLOG_SORT_BUTTON}"]', settle=1.5):
                raise ReviewsUnavailable("reviews pane did not render a sort control")
            if not self._click(f'[role=menuitemradio][data-index="{SORT_INDEX[self.sort]}"]', settle=3.0):
                raise ReviewsGated("choosing a review order was refused (sort menu did not open; sign-in dialog?)")
        if self.query:
            if not self._click(f'button[jslog^="{JSLOG_SEARCH_BUTTON}"]', settle=1.5):
                raise ReviewsUnavailable("reviews pane has no search control")
            box = self._box(f'input[jslog^="{JSLOG_SEARCH_INPUT}"]')
            if not box:
                raise ReviewsGated("the review search field did not open")
            x = float(box["x"]) + float(box.get("width") or 0) / 2
            y = float(box["y"]) + float(box.get("height") or 0) / 2
            res = self._run([command("type_at", x=x, y=y, text=self.query),
                             command("press", keys="enter"), command("sleep", wait_time=3.0)], timeout=90)
            if not res.steps or not res.steps[0].success:
                raise ReviewsGated("the review search field did not accept the keyword")
        if self.sort != "mostRelevant" or self.query:
            self._verify_order()

    def _verify_order(self, rounds: int = 2) -> None:
        for _ in range(rounds):
            pages = self.harvest() or self.scroll_round(scrolls=3)
            if pages:
                self._prefetched = pages
                return
        if self.limited_view():
            raise ReviewsGated(LIMITED_VIEW_REASON)
        raise ReviewsGated("no page of the chosen review order arrived after choosing it")

    # ------------------------------------------------------------- paging
    def harvest(self) -> list[ReviewsPage]:
        since = self._har_since
        return self._read_dump(self._run([self._dump_cmd(since)], timeout=180), since)

    def _ab_switch(self) -> None:
        """The wait-mode comparison is a script-driver experiment (its hook times requests)."""

    def scroll_round(self, scrolls: int | None = None) -> list[ReviewsPage]:
        cmds = []
        for _ in range(scrolls or self.scrolls_per_command):
            cmds += [command("scroll", clicks=WHEEL_CLICKS, x=PANE_X, y=PANE_Y),
                     command("sleep", wait_time=self.scroll_wait)]
        since = self._har_since
        cmds.append(self._dump_cmd(since))
        t0 = time.time()
        res = self._run(cmds, timeout=90 + (scrolls or self.scrolls_per_command) * (self.scroll_wait + 5))
        t1 = time.time()
        pages = self._read_dump(res, since)
        self._note_round(res, t1 - t0, time.time() - t1, len(pages))
        return pages
