"""Website contact enrichment through bro (one task per domain per run).

The homepage and up to ``bro.maxWebsitePages - 1`` same-site pages that look
like Impressum / contact / about are opened in the bro browser; text and
links are read in the page.  Results are stored per domain and attributed to
every place of the run that uses the domain.  When several places share a
domain (a chain), website phones are not attributed to a branch: they are
reported as uncertain unless they equal the branch's own Maps phone.

Pages are read once: the same address (only the fragment and the case of the
scheme and host do not count), or an address whose page was already reached by
a redirect.  Spelling alone never makes two addresses one page: ``/contact``
and ``/contact/`` are read both unless one redirects to the other.  A page the
site answers "not found" is not read a second time.  After two page opens in a
row fail, the domain's other pages are not opened.  A page that failed or was
not opened makes the section ``limited`` with the addresses and reasons (the
contacts of the pages read are kept).  Errors of the session itself (an
outcome bro does not know, a dead session, a block, a consent wall, a stop)
are not page failures: they reach the worker, which replaces the session and
queues the task again, and nothing of the domain is stored.

A domain's result belongs to every place that uses it, also one whose card is read later: the result
is attributed to the places linked to the domain at the moment it is stored (in that transaction), and
a place that joins a domain already read takes the stored result -- its status and reason -- without a
new read (:func:`attach_late_place`).  When a domain becomes shared, every place's contacts of it are
attributed again as a shared domain's (rc26 review).

``bro.websiteReadMode``: ``open`` (default) opens every page in the browser and
waits for it to load (45-55 s on heavy sites); ``fetch`` opens the homepage so,
then reads the other pages of its origin by a same-origin GET from it and their
HTML (``DOMParser``) -- a page that comes back short (built by its scripts),
fails or lies on another origin is opened as before.  Every page records how it
was read (``readBy``).
"""

from __future__ import annotations

import json
import logging
import urllib.parse as up
from typing import Any, Literal

from ..bro.transport import BroAuthError, BroCreateFailed, BroOutcomeUnknown, BroSessionDead, command
from ..maps.client import MapsBlocked, MapsClient, MapsConsentError
from ..pipeline.context import RunContext
from ..pipeline.discovery import NeedFreshSession, TaskInterrupted
from ..pipeline.filters import is_big_chain
from ..store import dumps, loads, now_iso
from .contacts import candidate_pages, emails_in, parse_impressum, phones_in, socials_in, to_e164  # noqa: F401

log = logging.getLogger(__name__)

PARSER_VERSION = "contacts-2026.10.14"  # social links: page provenance, same ownership rule as emails

READ_PAGE_JS = r"""(() => {
  const text = document.body ? document.body.innerText : '';
  const links = Array.from(document.querySelectorAll('a[href]')).slice(0, 3000).map(a => ({href: a.href, text: (a.innerText || '').trim().slice(0, 80)}));
  return {url: location.href, title: document.title, text: text.slice(0, 400000), links: links};
})()"""


#: title of the browser's own error page (connection or TLS failure, 5xx from the egress)
BROWSER_ERROR_TITLE = "Page Load Error"

STANDARD_PATHS = ["impressum", "kontakt", "contact", "imprint", "about", "legal-notice"]

#: page opens failing in a row after which the domain's other pages are not opened
MAX_FAILED_OPENS = 2

#: errors of the session, not of a page: the worker replaces the session and queues the task again
SESSION_ERRORS = (BroOutcomeUnknown, BroSessionDead, BroAuthError, BroCreateFailed, MapsBlocked, MapsConsentError,
                  NeedFreshSession, TaskInterrupted)

#: same-origin GETs of the other pages from the homepage (``fetch`` mode): text and links from the HTML.
#: As the browser shows it: blocks and line breaks separate the text (``textContent`` alone glues
#: "info@x.de" to the next block), and Cloudflare-protected addresses are decoded as the site's own
#: script does in the browser (``data-cfemail`` / ``/cdn-cgi/l/email-protection#``).
FETCH_PAGES_JS = r"""(() => {
  const BLOCKS = 'p, div, li, tr, td, th, h1, h2, h3, h4, h5, h6, section, article, header, footer, address, dt, dd, ' +
                 'table, ul, ol, nav, aside, main, form, label, button, blockquote, figure, figcaption, pre';
  const out = [];
  for (const u of %s) {
    const t0 = Date.now();
    try {
      const x = new XMLHttpRequest();
      x.open('GET', u, false);
      x.send();
      const type = x.getResponseHeader('content-type') || '';
      if (x.status < 200 || x.status >= 300 || !/html/i.test(type)) {
        out.push({url: u, requested: u, status: x.status, type: type, ms: Date.now() - t0});
        continue;
      }
      const doc = new DOMParser().parseFromString(x.responseText, 'text/html');
      doc.querySelectorAll('script, style, noscript, template').forEach(e => e.remove());
      const cf = h => { const k = parseInt(h.substr(0, 2), 16); let r = '';
        for (let i = 2; i < h.length; i += 2) r += String.fromCharCode(parseInt(h.substr(i, 2), 16) ^ k); return r; };
      doc.querySelectorAll('[data-cfemail]').forEach(e => { try { e.textContent = cf(e.getAttribute('data-cfemail')); } catch (err) {} });
      doc.querySelectorAll('a[href*="/cdn-cgi/l/email-protection#"]').forEach(a => {
        try { a.setAttribute('href', 'mailto:' + cf(a.getAttribute('href').split('#')[1])); } catch (err) {} });
      doc.querySelectorAll('br').forEach(e => e.replaceWith('\n'));
      doc.querySelectorAll(BLOCKS).forEach(e => e.append('\n'));
      const base = x.responseURL || u;
      const links = Array.from(doc.querySelectorAll('a[href]')).slice(0, 3000).map(a => {
        let href = '';
        try { href = new URL(a.getAttribute('href'), base).href; } catch (e) {}
        return {href: href, text: (a.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80)};
      }).filter(l => l.href);
      const text = doc.body ? (doc.body.innerText || doc.body.textContent || '') : '';
      out.push({url: base, requested: u, status: x.status, title: doc.title, text: text.slice(0, 400000),
                links: links, ms: Date.now() - t0});
    } catch (e) {
      out.push({url: u, requested: u, status: 0, error: String(e), ms: Date.now() - t0});
    }
  }
  return out;
})()"""


def page_key(url: str) -> str:
    """An address as such: scheme and host in lower case, an empty path as ``/``, no fragment.  Nothing
    else is assumed equal (``/contact`` and ``/contact/``, ``www.`` or not, may serve other pages)."""
    p = up.urlparse(url or "")
    return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path or '/'}{'?' + p.query if p.query else ''}"


def _read(maps: MapsClient, url: str) -> dict[str, Any] | None:
    res = maps.browser.execute([command("open_url", url=url), command("sleep", wait_time=1.5),
                                command("run_js", js_code=READ_PAGE_JS, out_type="dict", timeout=60)], timeout=150)
    maps.browser.current_url = None
    if not res.steps or not res.steps[0].success:
        step = res.steps[0] if res.steps else None
        hint = (" (the browser opens it over HTTPS only: a site served only over plain HTTP cannot be opened,"
                " and a site that does not answer over HTTPS fails the same way)") if url.startswith("http://") else ""
        raise RuntimeError(f"open failed{hint}: {step.error_name if step else res.status} "
                           f"{(step.error_message if step else '') or ''}"[:300])
    last = res.steps[-1]
    page = (last.data or {}).get("result") if last.success else None
    if page is not None and page.get("title") == BROWSER_ERROR_TITLE:
        # the bro browser upgrades every URL to HTTPS; sites served only over plain HTTP end here
        first = " ".join((page.get("text") or "").split())[:160]
        raise RuntimeError(f"browser could not load the page{' (opened over HTTPS only)' if url.startswith('http://') else ''}: {first}")
    if page is not None and not _not_found(page) and (len(page.get("text") or "") < 300 or not page.get("links")):
        # script-rendered sites: give the page time and read again
        res = maps.browser.execute([command("sleep", wait_time=3.0),
                                    command("run_js", js_code=READ_PAGE_JS, out_type="dict", timeout=60)], timeout=90)
        again = (res.steps[-1].data or {}).get("result") if res.steps and res.steps[-1].success else None
        if again and len(again.get("text") or "") >= len(page.get("text") or ""):
            page = again
    if page is None:
        raise RuntimeError(f"the page opened but could not be read: {(last.error_name or '')} {(last.error_message or '')}"[:300])
    page["readBy"] = "open"
    return page


def _fetch_pages(maps: MapsClient, urls: list[str]) -> dict[str, dict[str, Any]]:
    """``fetch`` mode: the other pages read by same-origin GETs from the homepage.  Returned by requested
    URL: a full HTML page (not short, with links), or ``{"notFound": True}`` for a 404; anything else is
    left out -- and opened."""
    if not urls:
        return {}
    res = maps.browser.execute([command("run_js", js_code=FETCH_PAGES_JS % json.dumps(urls), out_type="list",
                                        timeout=150)], timeout=180)
    step = res.steps[0] if res.steps else None
    rows = (step.data or {}).get("result") if step is not None and step.success and isinstance(step.data, dict) else None
    out: dict[str, dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        status = int(row.get("status") or 0)
        if status == 404:
            out[row.get("requested")] = {"notFound": True}
        elif 200 <= status < 300 and len(row.get("text") or "") >= 300 and row.get("links"):
            out[row.get("requested")] = {"url": row.get("url"), "title": row.get("title"), "text": row.get("text"),
                                         "links": row.get("links"), "readBy": "fetch"}
    return out


def _not_found(page: dict[str, Any]) -> bool:
    """Guessed standard paths often answer with a 404 page; those carry no contacts."""
    title = (page.get("title") or "").lower()
    text = (page.get("text") or "")[:400].lower()
    return any(m in title or m in text for m in ("404", "not found", "nicht gefunden", "page introuvable"))


BIG_CHAIN_REASON = "big chain (excluded like Apify)"
SOCIAL_PROFILE_REASON = "website is a social profile; not crawled"


def domain_places(db, domain: str) -> list[str]:
    """The places whose contacts come from ``domain`` (read in the caller's transaction)."""
    return sorted({r[0] for r in db.execute(
        "SELECT fid FROM sections WHERE section = 'contacts' AND json_extract(detail, '$.domain') = ?", (domain,))})


def store_domain_result(store, db, domain: str, data: dict, status: str, reason: str | None, detail: dict) -> list[str]:
    """Attribute a domain's contacts to every place linked to it now, as a shared domain's when there are
    several, and give each the section status; the places' earlier contacts of the domain are replaced
    (a place's ownership changes when the domain becomes shared)."""
    fids = domain_places(db, domain)
    shared = len(fids) > 1
    if fids:
        db.execute(f"DELETE FROM contacts WHERE domain = ? AND fid IN ({','.join('?' * len(fids))})", (domain, *fids))
    for fid in fids:
        place = store.place(fid)
        own_phone = country = None
        if place is not None:
            details = loads(place["details"]) or loads(place["search_data"]) or {}
            own_phone = (details.get("phoneUnformatted") or "").replace(" ", "") or None
            country = details.get("countryCode")
        _attribute(store, db, fid, domain, _localised(data, country), shared=shared, own_phone=own_phone)
        store.set_section(fid, "contacts", status, reason=reason, count=len(data["emails"]) + len(data["phones"]),
                          detail={**detail, "domain": domain, "shared": shared}, db=db)
    return fids


def attach_late_place(ctx: RunContext, db, fid: str, domain: str) -> str:
    """A place joins a domain whose website task already exists (its card was read later): it gets the
    stored result if the domain was read, the domain's failure if it failed; while the task is queued or
    running nothing is needed (the result is attributed when stored).  Returns what was done."""
    store = ctx.store
    if is_big_chain(domain):
        store.set_section(fid, "contacts", "absent", reason=BIG_CHAIN_REASON, db=db)
        return "big_chain"
    row = db.execute("SELECT status, data, error FROM domains WHERE domain = ?", (domain,)).fetchone()
    task = db.execute("SELECT status, last_error FROM tasks WHERE kind = 'website' AND key = ?", (domain,)).fetchone()
    if row is not None and row[0] in ("done", "limited") and row[1]:
        sibling = db.execute(
            "SELECT status, reason, detail FROM sections WHERE section = 'contacts' AND fid != ? AND "
            "json_extract(detail, '$.domain') = ? AND status IN ('done', 'limited') LIMIT 1", (fid, domain)).fetchone()
        status, reason = (sibling[0], sibling[1]) if sibling else (row[0], None)
        detail = {k: v for k, v in (loads(sibling[2]) if sibling else {}).items() if k not in ("domain", "shared")}
        store_domain_result(store, db, domain, loads(row[1]), status, reason, {**detail, "attachedLate": True})
        return "stored_result"
    if (row is not None and row[0] == "failed") or (task is not None and task[0] == "failed"):
        store.set_section(fid, "contacts", "failed", reason=((row[2] if row is not None else None) or
                                                             (task[1] if task is not None else None) or "")[:300], db=db)
        return "failed"
    return "pending_task"


def handle_website(ctx: RunContext, maps: MapsClient, task: Any) -> dict:
    store = ctx.store
    p = loads(task["payload"])
    domain, url = p["domain"], p["url"]
    if is_big_chain(domain):
        with store.tx() as db:
            for fid in domain_places(db, domain):
                store.set_section(fid, "contacts", "absent", reason=BIG_CHAIN_REASON, db=db)
        return {"domain": domain, "skipped": "big_chain"}
    social = socials_in([url])
    if any(social.values()):
        # the "website" is a social profile (Instagram, Facebook, ...): nothing to crawl there
        data = {"emails": [], "phones": [], "phonesUncertain": [], "socials": social, "socialSources": {},
                "impressum": None, "pages": []}
        with store.tx() as db:
            db.execute("INSERT OR REPLACE INTO domains (domain, status, pages, data, error, updated_at) VALUES (?, ?, 0, ?, NULL, ?)",
                       (domain, "done", dumps(data), now_iso()))
            store_domain_result(store, db, domain, data, "done", SOCIAL_PROFILE_REASON, {"pages": 0})
        return {"domain": domain, "socialProfile": True}
    max_pages = ctx.run.bro.maxWebsitePages
    mode: Literal["open", "fetch"] = ctx.run.bro.websiteReadMode
    pages: list[dict[str, Any]] = []
    errors: list[str] = []
    read_keys: set[str] = set()          # pages already read (requested and final URLs)
    skipped: list[str] = []              # pages left unopened after failed opens in a row
    failed: list[str] = []               # pages that could not be read: "<url>: <reason>"
    try:
        home = _read(maps, url)
    except SESSION_ERRORS:
        raise
    except Exception as exc:  # noqa: BLE001 - recorded, the domain fails as a whole
        home = None
        errors.append(str(exc))
    if home:
        pages.append(home)
        extras = candidate_pages(home.get("url") or url, home.get("links") or [], max_pages - 1)
        if not any(w in u.lower() for u in extras for w in ("impressum", "imprint", "legal", "kontakt", "contact")):
            base = (home.get("url") or url).split("?")[0].split("#")[0]
            root = base[:base.index("/", base.index("//") + 2)] if base.count("/") >= 3 else base.rstrip("/")
            extras += [f"{root}/{path}" for path in STANDARD_PATHS if f"{root}/{path}" not in extras]
            extras = extras[:max_pages - 1]
        read_keys |= {page_key(url), page_key(home.get("url") or url)}
        unique: dict[str, str] = {}
        for u in extras:
            if page_key(u) not in read_keys:
                unique.setdefault(page_key(u), u)
        extras = list(unique.values())
        fetched = _fetch_pages(maps, extras) if mode == "fetch" else {}
        failed_in_row = 0
        for i, extra in enumerate(extras):
            if ctx.should_stop():
                # a stop mid-domain returns the task to the queue: the pages read so far are not stored as
                # the domain's result (a resume reads it again; rc23 stored them as done)
                raise TaskInterrupted(ctx.stop_reason or "stop")
            if page_key(extra) in read_keys:
                continue                        # the final URL of an earlier page
            page = fetched.get(extra)
            if page and page.get("notFound"):
                continue
            if page is None:
                if failed_in_row >= MAX_FAILED_OPENS:
                    skipped = extras[i:]
                    break
                try:
                    page = _read(maps, extra)
                    failed_in_row = 0
                except SESSION_ERRORS:
                    raise
                except Exception as exc:  # noqa: BLE001 - this page fails, the others are read
                    errors.append(f"{extra}: {exc}")
                    failed.append(f"{extra}: {exc}"[:200])
                    failed_in_row += 1
                    continue
            if page and not _not_found(page):
                final = page_key(page.get("url") or extra)
                if final not in read_keys:
                    pages.append(page)
                read_keys |= {final, page_key(extra)}
        # links a page's scripts add (a footer of social profiles) are not in its HTML: when the pages read
        # name no social profile, the first page read from its HTML is opened in the browser as well
        by_fetch = [i for i, pg in enumerate(pages) if pg.get("readBy") == "fetch"]
        if by_fetch and ctx.should_stop():
            raise TaskInterrupted(ctx.stop_reason or "stop")
        if by_fetch and not any(
                any(socials_in([l.get("href") or "" for l in pg.get("links") or []]).values()) for pg in pages):
            first = pages[by_fetch[0]]
            try:
                opened = _read(maps, first.get("url"))
            except SESSION_ERRORS:
                raise
            except Exception as exc:  # noqa: BLE001 - the fetched page stays, its scripts' links unknown
                errors.append(f"{first.get('url')}: {exc}")
                failed.append(f"{first.get('url')} (opened to see the links its scripts add): {exc}"[:200])
                opened = None
            if opened and not _not_found(opened):
                opened["readBy"] = "open (no social profile in the HTML)"
                pages[by_fetch[0]] = opened
    if not pages:
        with store.tx() as db:
            db.execute("INSERT OR REPLACE INTO domains (domain, status, pages, data, error, updated_at) VALUES (?, ?, 0, NULL, ?, ?)",
                       (domain, "failed", "; ".join(errors)[:1000], now_iso()))
            for fid in domain_places(db, domain):
                store.set_section(fid, "contacts", "failed", reason="; ".join(errors)[:300], db=db)
        raise RuntimeError(f"website {domain} unreachable: {'; '.join(errors)[:300]}")
    for page in pages:
        ctx.evidence(kind="website", url=page.get("url") or url, status=200, text=dumps(page), task_id=task["id"])
    data = extract(pages)
    data["pageReads"] = [{"url": p.get("url"), "readBy": p.get("readBy") or "open"} for p in pages]
    status = "limited" if skipped or failed else "done"
    parts = []
    if failed:
        parts.append(f"{len(failed)} page(s) could not be read: " + "; ".join(failed))
    if skipped:
        parts.append(f"after {MAX_FAILED_OPENS} failed opens in a row {len(skipped)} page(s) not opened: "
                     + ", ".join(skipped))
    reason = " | ".join(parts)[:600] if parts else None
    with store.tx() as db:
        db.execute("INSERT OR REPLACE INTO domains (domain, status, pages, data, error, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                   (domain, status, len(pages), dumps(data), "; ".join(errors)[:1000] or None, now_iso()))
        # the places linked now, in this transaction: also those whose card was read during the crawl
        store_domain_result(store, db, domain, data, status, reason,
                            {"pages": len(pages), "readMode": mode, "fetched": sum(1 for p in pages if p.get("readBy") == "fetch"),
                             "failedPages": len(failed), "notOpened": len(skipped)})
    return {"domain": domain, "pages": len(pages), "emails": len(data["emails"]), "errors": len(errors),
            "status": status, "fetched": sum(1 for p in pages if p.get("readBy") == "fetch")}


def extract(pages: list[dict[str, Any]]) -> dict[str, Any]:
    emails: dict[str, str] = {}
    phones: dict[str, str] = {}
    uncertain: dict[str, str] = {}
    socials: dict[str, list[str]] = {}
    social_sources: dict[str, list[dict]] = {}
    impressum = None
    for page in pages:
        url = page.get("url") or ""
        text = page.get("text") or ""
        hrefs = [l.get("href") or "" for l in page.get("links") or []]
        for e in emails_in(text, hrefs):
            emails.setdefault(e, url)
        sure, maybe = phones_in(text, hrefs)
        for ph in sure:
            phones.setdefault(ph, url)
        for ph in maybe:
            uncertain.setdefault(ph, url)
        for key, values in socials_in(hrefs).items():
            bucket = socials.setdefault(key, [])
            bucket.extend(v for v in values if v not in bucket)
            for value in values:
                evidence = {"sourceUrl": url or None, "observedAt": page.get("observedAt") or now_iso()}
                sources = social_sources.setdefault(value, [])
                if not any(s["sourceUrl"] == evidence["sourceUrl"] for s in sources):
                    sources.append(evidence)
        path = up.urlparse(url).path.lower()
        if impressum is None and any(w in path for w in ("impressum", "imprint", "legal", "mentions", "aviso")):
            page_emails = emails_in(text, hrefs)
            page_phones, _ = phones_in(text, hrefs)
            impressum = parse_impressum(text, url, page_emails, page_phones)
    for ph in phones:
        uncertain.pop(ph, None)
    return {"emails": [{"value": e, "sourceUrl": u} for e, u in emails.items()],
            "phones": [{"value": p, "sourceUrl": u} for p, u in phones.items()],
            "phonesUncertain": [{"value": p, "sourceUrl": u} for p, u in uncertain.items()],
            "socials": socials, "socialSources": social_sources, "impressum": impressum,
            "pages": [p.get("url") for p in pages]}


def _localised(data: dict, country: str | None) -> dict:
    """Phones in E.164 using the place's country; duplicates of one number collapse."""
    out = dict(data)
    for key in ("phones", "phonesUncertain"):
        seen: dict[str, dict] = {}
        for p in data.get(key) or []:
            value = to_e164(p["value"], country)
            seen.setdefault(value, {**p, "value": value})
        out[key] = list(seen.values())
    sure = {p["value"] for p in out["phones"]}
    out["phonesUncertain"] = [p for p in out["phonesUncertain"] if p["value"] not in sure]
    return out


def _attribute(store, db, fid: str, domain: str, data: dict, *, shared: bool, own_phone: str | None) -> None:
    rows = []
    for e in data["emails"]:
        rows.append(("email", e["value"], e["sourceUrl"], "domain" if shared else "place"))
    for p in data["phones"]:
        branch = (not shared) or (own_phone and p["value"].lstrip("+") == own_phone.lstrip("+"))
        rows.append(("phone" if branch else "phoneUncertain", p["value"], p["sourceUrl"],
                     "place" if branch else "domain"))
    for p in data["phonesUncertain"]:
        rows.append(("phoneUncertain", p["value"], p["sourceUrl"], "domain"))
    for key, values in data["socials"].items():
        for v in values:
            sources = sorted((data.get("socialSources") or {}).get(v) or [], key=lambda s: s.get("sourceUrl") or "")
            source = next((s["sourceUrl"] for s in sources if s.get("sourceUrl")), None)
            # the same attribution as emails: the place's own domain, or a domain shared by several
            # places (a chain) -- a social link on a shared domain is not proven to be the branch's
            db.execute("INSERT INTO contacts (fid, kind, value, domain, source_url, context, ownership, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(fid, kind, value) DO UPDATE SET "
                       "source_url=COALESCE(excluded.source_url, contacts.source_url), "
                       "context=CASE WHEN excluded.source_url IS NOT NULL THEN excluded.context ELSE contacts.context END, "
                       "ownership=excluded.ownership",
                       (fid, key, v, domain, source, dumps({"sources": sources}), "domain" if shared else "place",
                        now_iso()))
    for kind, value, source, ownership in rows:
        db.execute("INSERT OR IGNORE INTO contacts (fid, kind, value, domain, source_url, context, ownership, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (fid, kind, value, domain, source, None, ownership, now_iso()))
    if data.get("impressum"):
        db.execute("INSERT OR REPLACE INTO contacts (fid, kind, value, domain, source_url, context, ownership, created_at) "
                   "VALUES (?, 'impressum', 'impressum', ?, ?, ?, ?, ?)",
                   (fid, domain, data["impressum"].get("url"), dumps(data["impressum"]), "domain" if shared else "place",
                    now_iso()))
