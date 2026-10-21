"""Optional sections outside the Maps endpoints: web results (Google Search).

Apify's ``webResults`` are the organic results of a Google web search for the
place; the Maps place panel only shows an empty "Web results" header (probed
2026-10-01).  The search page is rendered in the bro browser and its organic
result blocks are read from the DOM (no HTTP status is known: evidence records 0).

Outcomes of the ``webResults`` section:

* ``done`` -- organic results were read (their query, country, language, ranks and links kept);
* ``empty`` -- only on positive evidence: the page shows Google's "did not match any documents"
  notice (``emptyBasis: no_results_notice``; the notice in English and the other languages of
  NO_RESULTS).  An empty result container alone is not evidence: results render asynchronously;
* a consent page is passed with "Reject all" (as for Maps) and the search opened again; a page still
  asking raises :class:`MapsConsentError` (the worker replaces the session);
* a page that never became ready raises: the task is retried and, once its attempts are spent,
  ends ``failed`` with its section (``--retry-failed`` picks it up);
* a block page ends the section as ``limited`` and raises :class:`MapsBlocked`.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse as up
from typing import Any

from ..bro.transport import BroOutcomeUnknown, BroSessionDead, command
from ..maps.client import MapsBlocked, MapsClient, MapsConsentError
from ..store import dumps, loads, now_iso
from .context import RunContext

log = logging.getLogger(__name__)

PARSER_VERSION = "web-results-2026.10.13"
#: DOM observations of one opened search page before it counts as not ready (waits of 1, 2, 3 s)
OBSERVATIONS = 4
#: Google's no-results notice ("Your search ... did not match any documents") in the languages it is
#: matched in -- only ever read when no organic result was found; a notice not listed here makes an
#: empty page a task failure (retried), never an empty result
NO_RESULTS = ("did not match any documents", "No results found for", "übereinstimmenden Dokumente gefunden",
              "ergab keine Treffer", "ne correspond à aucun document", "no ha obtenido ningún resultado",
              "no obtuvo ningún resultado", "non ha prodotto risultati", "geen overeenkomstige documenten",
              "não encontrou nenhum documento")

# One-line JS after folding (run_js): block comments only.
SERP_JS = r"""(() => {
  const container = document.querySelector('#rso') || document.querySelector('#search');
  const root = container || document.body;
  const out = [];
  const seen = new Set();
  for (const h3 of root.querySelectorAll('a h3, h3 a')) {
    const a = h3.closest('a') || h3.querySelector('a');
    if (!a || !a.href || seen.has(a.href)) continue;
    if (a.closest('[data-text-ad], #tads, #bottomads')) continue;
    let link;
    try { link = new URL(a.href, location.href); } catch (e) { continue; }
    if (!/^https?:$/.test(link.protocol)) continue;
    const google = /^(www\.)?google\.[a-z.]+$/.test(link.hostname);
    /* Google's /goto and /url redirects are kept as observed: their token is not a destination URL. */
    const redirect = google && ((link.pathname === '/goto' && link.searchParams.has('url')) ||
                                (link.pathname === '/url' && (link.searchParams.has('q') || link.searchParams.has('url'))));
    if (google && !redirect) continue;
    seen.add(a.href);
    /* one organic result per top-level block of the result list; further titled links inside the
       same block are its sitelinks ("Menu", "Book Now"), kept but marked */
    const top = container ? a.closest('#rso > div, #search > div > div') : null;
    const kids = top && top.parentElement && top.parentElement.children ? [...top.parentElement.children] : null;
    const group = kids ? kids.indexOf(top) : out.length;
    const main = !out.some(r => r.group === group);
    let block = a.closest('div[data-hveid]') || a.parentElement;
    for (let i = 0; i < 4 && block && block.innerText.length < 80; i++) block = block.parentElement;
    const cite = block ? block.querySelector('cite') : null;
    const snip = block ? block.querySelector('[data-sncf], .VwiC3b, [style*="-webkit-line-clamp"]') : null;
    out.push({title: h3.innerText.trim(), url: a.href, displayedUrl: cite ? cite.innerText.trim() : null,
              description: snip ? snip.innerText.trim() : null, group: group, main: main});
    if (out.length >= 20) break;
  }
  const isRedirect = (u) => /^https?:\/\/(www\.)?google\.[^/]+\/(goto|url)\?/.test(u);
  const text = root.innerText || '';
  const html = root.outerHTML || '';
  const noResults = __NO_RESULTS__.some(p => text.includes(p));
  return {
    url: location.href, results: out,
    resultLinks: out.map(r => ({url: r.url, kind: isRedirect(r.url) ? 'google_redirect' : 'direct',
                                targetUrl: isRedirect(r.url) ? null : r.url})),
    readyState: document.readyState, container: !!container,
    ready: document.readyState === 'complete' && (out.length > 0 || noResults),
    noResults: noResults,
    text: text.slice(0, 120000), html: html.slice(0, 1048576), domTruncated: html.length > 1048576,
    blocked: /\/sorry\//.test(location.href) || !!document.querySelector('#captcha-form, form[action*="sorry"]'),
    consent: /consent\.google\./.test(location.hostname)
  };
})()""".replace("__NO_RESULTS__", json.dumps(list(NO_RESULTS), ensure_ascii=False))


def _observe(maps: MapsClient, commands: list[dict], timeout: float) -> Any:
    res = maps.browser.execute(commands, timeout=timeout)
    step = res.steps[-1] if res.steps else None
    if step is None or not step.success:
        raise RuntimeError(f"web search observation failed: {step.error_name if step else res.status}")
    return (step.data or {}).get("result")


def _open(maps: MapsClient, url: str) -> Any:
    out = _observe(maps, [command("open_url", url=url), command("sleep", wait_time=1.0),
                          command("run_js", js_code=SERP_JS, out_type="dict", timeout=60)], 150)
    maps.browser.current_url = None   # the session left Maps
    return out


def _poll(maps: MapsClient, wait: float) -> Any:
    return _observe(maps, [command("sleep", wait_time=wait),
                           command("run_js", js_code=SERP_JS, out_type="dict", timeout=30)], 60 + wait)


def handle_webresults(ctx: RunContext, maps: MapsClient, task: Any) -> dict:
    store = ctx.store
    fid = loads(task["payload"])["fid"]
    place = store.place(fid)
    data = (loads(place["details"]) or loads(place["search_data"]) or {}) if place else {}
    query = " ".join(x for x in (data.get("title"), data.get("address")) if x)
    if not query:
        store.set_section(fid, "webResults", "absent", reason="no title/address to search for")
        return {"fid": fid, "results": 0}
    url = f"https://www.google.com/search?q={up.quote(query)}&hl={ctx.hl}&gl={ctx.gl}"
    out = _open(maps, url)
    consent_passed = False
    seen = 0
    empty_basis = None
    while True:
        if not isinstance(out, dict) or not isinstance(out.get("results"), list):
            ctx.evidence(kind="web_results_dom", url=url, status=0, text=dumps(out), task_id=task["id"], failed=True,
                         note="DOM observation, HTTP status unknown; malformed result")
            raise RuntimeError("malformed web search DOM observation")
        seen += 1
        if out.get("ready") and not out["results"] and out.get("noResults"):
            empty_basis = "no_results_notice"
        settled = bool((out.get("ready") and out["results"]) or empty_basis)
        ctx.evidence(kind="web_results_dom", url=str(out.get("url") or url), status=0, text=dumps(out),
                     task_id=task["id"], failed=not settled or bool(out.get("blocked") or out.get("consent")),
                     keep=bool(empty_basis), note="DOM observation, HTTP status unknown")
        if out.get("blocked") or settled:
            break
        if out.get("consent"):
            if consent_passed:
                raise MapsConsentError("Google Search still asks for consent after 'Reject all'")
            maps._reject_consent()
            consent_passed = True
            out = _open(maps, url)
            seen = 0
            continue
        if seen >= OBSERVATIONS:
            break
        out = _poll(maps, float(seen))
    context = {"query": query, "country": ctx.gl, "language": ctx.hl, "observedAt": now_iso(),
               "source": "google_search_dom", "sourceUrl": out.get("url") or url, "parserVersion": PARSER_VERSION}
    if out.get("blocked"):
        store.set_section(fid, "webResults", "limited", reason="Google Search block page",
                          detail={**context, "results": []})
        raise MapsBlocked("Google Search answered with a block page")
    if out.get("consent"):
        raise MapsConsentError("Google Search still asks for consent")
    if empty_basis:
        store.set_section(fid, "webResults", "empty", count=0,
                          reason="the search page holds no organic result",
                          detail={**context, "results": [], "emptyBasis": empty_basis})
        return {"fid": fid, "results": 0, "empty": empty_basis}
    if not out.get("ready"):
        raise RuntimeError(f"web search page not ready after {OBSERVATIONS} observations "
                           f"(readyState {out.get('readyState')}, container {bool(out.get('container'))})")
    results = out.get("results") or []
    if any(not isinstance(r, dict) or not isinstance(r.get("url"), str) for r in results):
        raise RuntimeError("malformed web search result rows")
    links = out.get("resultLinks") or []
    run = getattr(ctx, "run", None)
    if run is not None and run.bro.core_contract:
        # Apify reports the first APIFY_RESULTS organic results with their destination: Google's links are
        # encrypted /goto redirects, so each is followed once, as a click (the page's own link, never edited)
        resolved = resolve_destinations(maps, [r for r in results if r.get("main", True)][:APIFY_RESULTS])
        for link in links:
            if link.get("url") in resolved:
                link["targetUrl"], link["resolvedBy"] = resolved[link["url"]], "followed"
    store.set_section(fid, "webResults", "done", count=len(results), detail={
        **context, "results": results, "resultRanks": [{"rank": i + 1, "url": r.get("url")} for i, r in enumerate(results)],
        "resultLinks": links})
    return {"fid": fid, "results": len(results)}


#: organic results Apify reports per place (stored runs 2026-10-09/10: 3, or none)
APIFY_RESULTS = 3


def resolve_destinations(maps: MapsClient, results: list[dict]) -> dict[str, str]:
    """Destination of each Google redirect among ``results``: the link is opened as a click and the
    address the browser lands on is read.  A failed or Google-bound landing leaves it unresolved."""
    out: dict[str, str] = {}
    for r in results:
        url = r.get("url") or ""
        if not re.match(r"^https?://(www\.)?google\.[^/]+/(goto|url)\?", url):
            continue
        try:
            res = maps.browser.execute([command("open_url", url=url), command("get_url")], timeout=90)
        except (BroOutcomeUnknown, BroSessionDead):
            raise
        except Exception:  # noqa: BLE001 - a slow or broken destination leaves the link as observed
            log.info("web result destination not reached: %s", url[:120])
            continue
        finally:
            maps.browser.current_url = None
        step = res.steps[-1] if getattr(res, "steps", None) else None
        landed = ((step.data or {}).get("url") if step is not None and step.success else None) or ""
        host = up.urlsplit(landed).hostname or ""
        if landed.startswith("http") and not re.match(r"^(www\.)?google\.", host):
            out[url] = landed
    return out
