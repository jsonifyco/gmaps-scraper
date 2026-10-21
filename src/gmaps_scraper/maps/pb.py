"""Request builders for the Google Maps web endpoints.

The ``pb`` parameter is Google's protobuf-in-URL encoding: ``!<field><type><value>``
where ``m<n>`` opens a message of ``n`` following tokens.  The long tails below
were captured from the Maps web client;
only the leading viewport/query/paging tokens are filled in by this module.

Endpoints confirmed in the stage-1 probes:

* search  ``GET /search?tbm=map&...&pb=...``  -- 20 places per page, ``!8i<offset>``
  pages; a single viewport ends after ~165 places (empty ``[64]``).
* place   ``GET /maps/preview/place?...&pb=!1m14!1s<fid>...`` -- full place array.
* photos  ``POST /maps/_/MapsWizUi/data/batchexecute?rpcids=hspqX`` -- must be sent
  from the page (XHR); paging token at ``inner[4][2][2]``.
* reviews ``POST batchexecute?rpcids=qv9Egd`` -- carries a content-bound
  ``X-maps-bgkey`` header, so only the page's own requests are answered; the
  scraper drives the reviews pane and reads those responses.
* Q&A     ``POST /local/place/qa/_/LocalPlaceqaFrontendUi/data/batchexecute`` -- Google's local
  Q&A app (the frame Maps opens for "More questions"); unsigned, sent from a www.google.com page
  (maps/questions.py, 2026-10-13).
"""

from __future__ import annotations

import json
import math
import random
import string
import urllib.parse as up

BASE = "https://www.google.com"

#: vertical field of view (degrees) encoded as ``!4f13.1``; the viewport
#: height in metres is ``altitude * 2 * tan(fov / 2)`` for the 768 px screen
FOV_DEG = 13.1
SCREEN_W, SCREEN_H = 1024, 768
_HEIGHT_PER_ALTITUDE = 2 * math.tan(math.radians(FOV_DEG / 2))

SEARCH_TAIL = (
    "!10b1!12m62!1m5!18b1!30b1!31m1!1b1!34e1!2m4!5m1!6e2!20e3!39b1!6m33!32i1!49b1!63m0!66b1!85b1"
    "!114b1!149b1!206b1!209b1!212b1!215b1!216b1!222b1!223b1!232b1!234b1!235b1!239b1!246b1!253b1"
    "!260b1!262b1!266b1!270b1!271b1!273b1!277b1!280b1!281b1!291m0!294b1!302i300!303i100!10b1!12b1"
    "!13b1!14b1!16b1!17m1!3e1!20m3!5e2!6b1!14b1!46m1!1b0!96b1!97m1!2b1!99b1!19m4!2m3!1i360!2i120"
    "!4i8!20m57!2m2!1i203!2i100!3m2!2i4!5b1!6m6!1m2!1i86!2i86!1m2!1i408!2i240!7m33!1m3!1e1!2b0"
    "!3e3!1m3!1e2!2b1!3e2!1m3!1e2!2b0!3e3!1m3!1e8!2b0!3e3!1m3!1e10!2b0!3e3!1m3!1e10!2b1!3e2!1m3"
    "!1e10!2b0!3e4!1m3!1e9!2b1!3e2!2b1!9b0!15m8!1m7!1m2!1m1!1e2!2m2!1i195!2i195!3i20!22m5!1s{ei}"
    "!7e81!14m1!3s{ei}!15i9937!24m107!1m25!13m9!2b1!3b1!4b1!6i1!8b1!9b1!14b1!20b1!25b1!18m14!3b1"
    "!4b1!5b1!6b1!13b1!14b1!17b1!21b1!22b1!32b1!33m1!1b1!34b1!36e2!10m1!8e3!11m1!3e1!17b1!20m2"
    "!1e3!1e6!24b1!25b1!26b1!27b1!29b1!30m1!2b1!36b1!37b1!39m3!2m2!2i1!3i1!43b1!52b1!54m1!1b1"
    "!55b1!56m1!1b1!61m2!1m1!1e1!65m5!3m4!1m3!1m2!1i224!2i298!72m22!1m8!2b1!5b1!7b1!12m4!1b1!2b1"
    "!4m1!1e1!4b1!8m10!1m6!4m1!1e1!4m1!1e3!4m1!1e4!3sother_user_google_review_posts__and__hotel_and_vr_partner_review_posts"
    "!6m1!1e1!9b1!89b1!90m2!1m1!1e2!98m3!1b1!2b1!3b1!103b1!113b1!114m3!1b1!2m1!1b1!117b1!122m1!1b1"
    "!126b1!127b1!128m1!1b1!26m4!2m3!1i80!2i92!4i8!30m28!1m6!1m2!1i0!2i0!2m2!1i530!2i768!1m6!1m2"
    "!1i974!2i0!2m2!1i1024!2i768!1m6!1m2!1i0!2i0!2m2!1i1024!2i20!1m6!1m2!1i0!2i748!2m2!1i1024!2i768"
    "!34m19!2b1!3b1!4b1!6b1!8m6!1b1!3b1!4b1!5b1!6b1!7b1!9b1!12b1!14b1!20b1!23b1!25b1!26b1!31b1"
    "!37m1!1e81!42b1!49m10!3b1!6m2!1b1!2b1!7m2!1e3!2b1!8b1!9b1!10e2!50m3!2e2!3m1!3b1!61b1!67m5"
    "!7b1!10b1!14b1!15m1!1b0!69i797!77b1"
)

PLACE_TAIL = (
    "!12m4!2m3!1i360!2i120!4i8!13m57!2m2!1i203!2i100!3m2!2i4!5b1!6m6!1m2!1i86!2i86!1m2!1i408!2i240"
    "!7m33!1m3!1e1!2b0!3e3!1m3!1e2!2b1!3e2!1m3!1e2!2b0!3e3!1m3!1e8!2b0!3e3!1m3!1e10!2b0!3e3!1m3"
    "!1e10!2b1!3e2!1m3!1e10!2b0!3e4!1m3!1e9!2b1!3e2!2b1!9b0!15m8!1m7!1m2!1m1!1e2!2m2!1i195!2i195"
    "!3i20!14m3!1s{ei}!7e81!15i10112!15m108!1m26!13m9!2b1!3b1!4b1!6i1!8b1!9b1!14b1!20b1!25b1!18m15"
    "!3b1!4b1!5b1!6b1!13b1!14b1!17b1!21b1!22b1!30b1!32b1!33m1!1b1!34b1!36e2!10m1!8e3!11m1!3e1"
    "!17b1!20m2!1e3!1e6!24b1!25b1!26b1!27b1!29b1!30m1!2b1!36b1!37b1!39m3!2m2!2i1!3i1!43b1!52b1"
    "!54m1!1b1!55b1!56m1!1b1!61m2!1m1!1e1!65m5!3m4!1m3!1m2!1i224!2i298!72m22!1m8!2b1!5b1!7b1!12m4"
    "!1b1!2b1!4m1!1e1!4b1!8m10!1m6!4m1!1e1!4m1!1e3!4m1!1e4!3sother_user_google_review_posts__and__hotel_and_vr_partner_review_posts"
    "!6m1!1e1!9b1!89b1!90m2!1m1!1e2!98m3!1b1!2b1!3b1!103b1!113b1!114m3!1b1!2m1!1b1!117b1!122m1!1b1"
    "!126b1!127b1!128m1!1b1!21m0!22m1!1e81!30m8!3b1!6m2!1b1!2b1!7m2!1e3!2b1!9b1!34m5!7b1!10b1"
    "!14b1!15m1!1b0!37i797"
)

#: page size of the search endpoint (the web client asks for 20)
SEARCH_PAGE_SIZE = 20


def new_ei(rng: random.Random | None = None) -> str:
    """A client event id of the shape the web client sends (22 url-safe chars)."""
    rng = rng or random
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(rng.choice(alphabet) for _ in range(23))


def pb_escape(text: str) -> str:
    """Escape a string value for a ``pb`` token (``*`` and ``!`` are special)."""
    return text.replace("*", "*2A").replace("!", "*21")


def altitude_for_height(height_m: float) -> float:
    """Camera altitude (``!1d``) whose viewport is ``height_m`` metres tall."""
    return max(height_m, 1.0) / _HEIGHT_PER_ALTITUDE


def height_for_altitude(altitude: float) -> float:
    return altitude * _HEIGHT_PER_ALTITUDE


def altitude_for_zoom(zoom: float, lat: float) -> float:
    """Altitude matching a web-mercator zoom level at ``lat`` for the 768 px screen."""
    metres_per_px = 156543.03392 * math.cos(math.radians(lat)) / (2 ** zoom)
    return altitude_for_height(metres_per_px * SCREEN_H)


def zoom_for_altitude(altitude: float, lat: float) -> float:
    metres_per_px = height_for_altitude(altitude) / SCREEN_H
    return math.log2(156543.03392 * math.cos(math.radians(lat)) / max(metres_per_px, 1e-9))


def _num(value: float) -> str:
    text = repr(float(value))
    return text[:-2] if text.endswith(".0") else text


def search_pb(query: str, lat: float, lng: float, altitude: float, *, offset: int = 0,
              page_size: int = SEARCH_PAGE_SIZE, ei: str | None = None) -> str:
    head = (f"!1s{pb_escape(query)}!4m8!1m3!1d{_num(altitude)}!2d{_num(lng)}!3d{_num(lat)}"
            f"!3m2!1i{SCREEN_W}!2i{SCREEN_H}!4f{FOV_DEG}!7i{page_size}")
    if offset:
        head += f"!8i{offset}"
    return head + SEARCH_TAIL.format(ei=ei or new_ei())


def search_url(query: str, lat: float, lng: float, altitude: float, *, hl: str, gl: str,
               offset: int = 0, ei: str | None = None) -> str:
    params = {"tbm": "map", "authuser": "0", "hl": hl, "gl": gl, "q": query,
              "pb": search_pb(query, lat, lng, altitude, offset=offset, ei=ei)}
    return f"{BASE}/search?" + up.urlencode(params, quote_via=up.quote)


def place_pb(fid: str, *, lat: float | None = None, lng: float | None = None,
             altitude: float | None = None, ei: str | None = None) -> str:
    if lat is None or lng is None:
        lat, lng, altitude = 37.0625, -95.677068, 2.608160252827102e7
    altitude = altitude or 3000.0
    head = (f"!1m14!1s{fid}!3m12!1m3!1d{_num(altitude)}!2d{_num(lng)}!3d{_num(lat)}"
            f"!2m3!1f0.0!2f0.0!3f0.0!3m2!1i{SCREEN_W}!2i{SCREEN_H}!4f{FOV_DEG}")
    return head + PLACE_TAIL.format(ei=ei or new_ei())


def place_url(fid: str, *, hl: str, gl: str, ei: str | None = None) -> str:
    params = {"authuser": "0", "hl": hl, "gl": gl, "pb": place_pb(fid, ei=ei)}
    return f"{BASE}/maps/preview/place?" + up.urlencode(params, quote_via=up.quote)


def place_page_url(fid: str, *, hl: str, gl: str, reviews: bool = False) -> str:
    """The interactive place page (``!9m1!1b1`` opens the reviews pane)."""
    data = f"!4m4!3m3!1s{fid}!9m1!1b1" if reviews else f"!4m2!3m1!1s{fid}"
    return f"{BASE}/maps/place/data={data}?hl={up.quote(hl)}&gl={up.quote(gl)}"


def maps_home_url(*, hl: str, gl: str) -> str:
    return f"{BASE}/maps?hl={up.quote(hl)}&gl={up.quote(gl)}"


def batchexecute_path(rpc: str, *, source_path: str, hl: str, gl: str, reqid: int) -> str:
    params = {"rpcids": rpc, "source-path": source_path, "hl": hl, "gl": gl,
              "_reqid": str(reqid), "rt": "c"}
    return "/maps/_/MapsWizUi/data/batchexecute?" + up.urlencode(params)


def batchexecute_body(rpc: str, inner: list, *, tag: str = "generic") -> str:
    outer = [[[rpc, json.dumps(inner, separators=(",", ":"), ensure_ascii=False), None, tag]]]
    return "f.req=" + up.quote(json.dumps(outer, separators=(",", ":"), ensure_ascii=False), safe="")


#: Google's local Q&A app (the frame the Maps client opens for "More questions")
QA_APP = "/local/place/qa"


def qa_batchexecute_path(rpc: str, fid: str, *, hl: str, reqid: int) -> str:
    """The Q&A app's RPC path, with the source path of the place's questions page (as the app sends it)."""
    params = {"rpcids": rpc, "source-path": f"{QA_APP}/question/{fid}", "hl": hl, "authuser": "0",
              "_reqid": str(reqid), "rt": "c"}
    return f"{QA_APP}/_/LocalPlaceqaFrontendUi/data/batchexecute?" + up.urlencode(params)


def qa_page_url(fid: str, *, hl: str) -> str:
    """The place's questions page of the Q&A app (what Maps frames; kept as the section's source URL)."""
    return f"{BASE}{QA_APP}/question/{fid}?" + up.urlencode({"authuser": "0", "hl": hl})


#: photo categories filter of the gallery request (as sent by the web client)
_PHOTO_FILTERS = [[1, 0, 3], [2, 1, 2], [2, 0, 3], [8, 0, 3], [10, 0, 3], [10, 1, 2], [10, 0, 4], [9, 1, 2]]


def photos_inner(fid: str, *, token: str | None = None, page_size: int = 100) -> list:
    """Inner request of the ``hspqX`` gallery RPC; ``token`` continues a gallery."""
    return [2, None, [fid, None, None, None, None, None, None, None, 0], None,
            [None, [203, 100], [None, page_size, token, None, 1], None, None, None,
             [_PHOTO_FILTERS, 1], None, 0, None, None, None, None, None, [[[[[[2]]], [195, 195], 20]]]],
            [None, None, None, None, None, None, 81], None, None, None, None, None, None, None, None, None,
            [None, 1, None, 1]]
