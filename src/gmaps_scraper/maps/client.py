"""Google Maps access through one bro browser.

The browser must stand on a ``www.google.com`` page before same-origin calls
work: ``fetch_json`` and page XHRs inherit the page's origin and cookies.
:meth:`MapsClient.ensure_maps` opens Maps and, on the EU consent wall,
declines optional cookies ("Reject all") -- the most privacy-preserving
choice and the one that keeps the session reproducible.

Responses are classified before parsing; a successful bro command is not
proof of data: HTTP status, guarded-JSON prefix and Google's "unusual
traffic" pages are all checked here.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from ..bro.transport import BroBrowser, CommandResult, command
from . import pb

log = logging.getLogger(__name__)

CONSENT_HOST = "consent.google."
SORRY_MARKERS = ("/sorry/index", "unusual traffic", "detected unusual", "g-recaptcha", "captcha-form")

#: "Reject all" in the languages the consent wall is most likely shown in
REJECT_LABELS = ["Reject all", "Alle ablehnen", "Tout refuser", "Rechazar todo", "Rifiuta tutto",
                 "Alles afwijzen", "Odrzuć wszystko", "Rejeitar tudo", "Avvisa alla", "Afvis alle",
                 "Hylkää kaikki", "Odmítnout vše", "Отклонить все", "Avvis alle", "Tümünü reddet"]

CONSENT_JS = r"""(() => {
  const labels = %s;
  const btns = Array.from(document.querySelectorAll('button, input[type=submit]'));
  let target = btns.find(b => b.getAttribute('jsname') === 'tWT92d');
  if (!target) target = btns.find(b => labels.includes((b.innerText || b.value || '').trim()) || labels.includes(b.getAttribute('aria-label') || ''));
  if (!target) {
    const form = Array.from(document.forms).find(f => /consent\.google\.[^/]+\/save/.test(f.action) &&
        Array.from(f.elements).some(e => e.name === 'set_eom' && e.value === 'true'));
    if (form) target = form.querySelector('button, input[type=submit]');
  }
  if (!target) return {clicked: false, buttons: btns.map(b => (b.innerText || b.value || '').trim()).slice(0, 12)};
  target.click();
  return {clicked: true, label: (target.innerText || target.value || '').trim()};
})()"""


class MapsBlocked(Exception):
    """Google answered with a block (429, captcha, 'unusual traffic')."""


class MapsConsentError(Exception):
    """The consent wall could not be passed."""


@dataclass
class HttpResult:
    url: str
    status: int
    text: str
    headers: dict[str, Any]
    elapsed: float | None = None
    error: str | None = None

    @property
    def blocked(self) -> bool:
        if self.status in (429,):
            return True
        if self.status in (302, 403) or (self.text and len(self.text) < 20000):
            low = (self.text or "")[:20000].lower()
            return any(m in low for m in SORRY_MARKERS)
        return False


class MapsClient:
    """Maps-specific operations on top of a :class:`BroBrowser`."""

    def __init__(self, browser: BroBrowser, *, hl: str, gl: str):
        self.browser = browser
        self.hl = hl
        self.gl = gl
        self.reqid = int(time.time()) % 90000 + 10000
        self.ready = False
        self.consent_rejected = False

    # ----------------------------------------------------------- navigation
    def _run(self, commands: list[dict[str, Any]], *, timeout: float = 180.0) -> CommandResult:
        return self.browser.execute(commands, timeout=timeout)

    def open(self, url: str, *, settle: float = 0.0, timeout: float = 120.0) -> str:
        cmds = [command("open_url", url=url)]
        if settle:
            cmds.append(command("sleep", wait_time=settle))
        cmds.append(command("get_url"))
        res = self._run(cmds, timeout=timeout)
        final = ""
        for step in res.steps:
            if step.command == "get_url" and step.success:
                final = str((step.data or {}).get("url") or "")
        if not res.ok:
            failed = next((s for s in res.steps if not s.success), None)
            raise RuntimeError(f"navigation to {url[:120]} failed: "
                               f"{failed.error_name if failed else res.error_name} "
                               f"{(failed.error_message if failed else res.error_message) or ''}"[:400])
        self.browser.current_url = final or url
        return self.browser.current_url

    def ensure_maps(self, *, force: bool = False) -> None:
        """Stand on a Maps page of www.google.com (passing the consent wall once)."""
        current = self.browser.current_url or ""
        if self.ready and not force and current.startswith("https://www.google.com/"):
            return
        url = self.open(pb.maps_home_url(hl=self.hl, gl=self.gl), settle=1.0)
        for _ in range(3):
            if CONSENT_HOST not in url:
                break
            self._reject_consent()
            url = self._current_url()
        if CONSENT_HOST in url:
            raise MapsConsentError(f"consent wall still shown at {url[:120]}")
        if "/sorry/" in url:
            raise MapsBlocked(f"Google answered with a block page: {url[:120]}")
        if not url.startswith("https://www.google.com/"):
            raise MapsConsentError(f"unexpected landing page {url[:120]}")
        self.ready = True

    def _reject_consent(self) -> None:
        js = CONSENT_JS % json.dumps(REJECT_LABELS, ensure_ascii=False)
        res = self._run([command("run_js", js_code=js, out_type="dict"), command("sleep", wait_time=3.0),
                         command("get_url")])
        outcome = (res.steps[0].data or {}).get("result") if res.steps else None
        log.info("consent wall: %s", outcome)
        if not (outcome or {}).get("clicked"):
            raise MapsConsentError(f"no 'Reject all' control on the consent page: {outcome}")
        self.consent_rejected = True
        self.browser.current_url = str((res.steps[-1].data or {}).get("url") or "")

    def _current_url(self) -> str:
        res = self._run([command("get_url")], timeout=60)
        url = str((res.steps[0].data or {}).get("url") or "") if res.steps else ""
        self.browser.current_url = url
        return url

    # ----------------------------------------------------------------- GETs
    def fetch_many(self, urls: list[str], *, timeout: float = 240.0) -> list[HttpResult]:
        """GET several same-origin URLs from the page in one bro command.

        Synchronous XHRs inside one ``run_js`` step: a batch of several
        ``fetch_json`` steps was seen to leave the command ``running`` forever
        on an idle session.
        """
        self.ensure_maps()
        js = ("(() => { const out = []; for (const u of %s) { const t0 = Date.now(); try { "
              "const x = new XMLHttpRequest(); x.open('GET', u, false); x.send(); "
              "out.push({s: x.status, t: x.responseText, ms: Date.now() - t0, "
              "ct: x.getResponseHeader('content-type')}); } "
              "catch (e) { out.push({s: 0, t: '', e: String(e), ms: Date.now() - t0}); } } return out; })()"
              ) % json.dumps(urls)
        res = self._run([command("run_js", js_code=js, out_type="list", timeout=min(timeout - 10, 170))],
                        timeout=timeout)
        step = res.steps[0] if res.steps else None
        if step is None or not step.success:
            err = f"{step.error_name}: {step.error_message}" if step else f"{res.status} {res.error_name}"
            return [HttpResult(u, 0, "", {}, error=err[:500]) for u in urls]
        rows = (step.data or {}).get("result") if isinstance(step.data, dict) else None
        if not isinstance(rows, list) or len(rows) != len(urls):
            return [HttpResult(u, 0, "", {}, error=f"unexpected run_js result: {str(step.data)[:200]}") for u in urls]
        out = [HttpResult(u, int(r.get("s") or 0), str(r.get("t") or ""), {"content-type": r.get("ct")},
                          elapsed=(r.get("ms") or 0) / 1000.0, error=r.get("e")) for u, r in zip(urls, rows)]
        if any(r.blocked for r in out):
            raise MapsBlocked("Google answered with a block page (429 / unusual traffic)")
        return out

    def fetch_isolated(self, url: str, *, timeout: float = 120.0) -> HttpResult:
        """One GET through bro's ``fetch_json`` (isolated world); a fallback path."""
        self.ensure_maps()
        res = self._run([command("fetch_json", url=url, timeout=60)], timeout=timeout)
        step = res.steps[0] if res.steps else None
        if step is None or not step.success:
            return HttpResult(url, 0, "", {}, error=(f"{step.error_name}: {step.error_message}" if step else res.status))
        result = (step.data or {}).get("result") if isinstance(step.data, dict) else None
        if not isinstance(result, dict):
            return HttpResult(url, 0, "", {}, error=f"unexpected fetch_json data: {str(step.data)[:200]}")
        out = HttpResult(url, int(result.get("status") or 0), str(result.get("text") or ""),
                         dict(result.get("headers") or {}), elapsed=step.duration)
        if out.blocked:
            raise MapsBlocked("Google answered with a block page (429 / unusual traffic)")
        return out

    # ---------------------------------------------------------------- POSTs
    def xhr_post_many(self, rpc: str, bodies: list[str], *, source_path: str = "/maps",
                      timeout: float = 120.0) -> list[HttpResult]:
        """POST ``batchexecute`` bodies from the page itself (synchronous XHRs)."""
        self.ensure_maps()
        self.reqid += 100000
        path = pb.batchexecute_path(rpc, source_path=source_path, hl=self.hl, gl=self.gl, reqid=self.reqid)
        js = ("(() => { const out = []; for (const b of %s) { try { const x = new XMLHttpRequest(); "
              "x.open('POST', %s, false); "
              "x.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded;charset=UTF-8'); "
              "x.setRequestHeader('X-Same-Domain', '1'); x.send(b); "
              "out.push({s: x.status, t: x.responseText}); } catch (e) { out.push({s: 0, t: '', e: String(e)}); } } "
              "return out; })()") % (json.dumps(bodies), json.dumps(path))
        res = self._run([command("run_js", js_code=js, out_type="list", timeout=min(timeout, 170))],
                        timeout=timeout)
        step = res.steps[0] if res.steps else None
        if step is None or not step.success:
            err = f"{step.error_name}: {step.error_message}" if step else f"{res.status}"
            return [HttpResult(path, 0, "", {}, error=err[:500]) for _ in bodies]
        rows = (step.data or {}).get("result") or []
        out = [HttpResult(path, int(r.get("s") or 0), str(r.get("t") or ""), {}, error=r.get("e")) for r in rows]
        if any(r.blocked for r in out):
            raise MapsBlocked("Google answered with a block page (429 / unusual traffic)")
        return out

    def post_many(self, calls: list[tuple[str, str]], *, timeout: float = 150.0) -> list[HttpResult]:
        """POST same-origin ``batchexecute`` bodies, each to its own path, from the page (synchronous XHRs
        in one bro command, as :meth:`fetch_many`)."""
        self.ensure_maps()
        js = ("(() => { const out = []; for (const [p, b] of %s) { const t0 = Date.now(); try { "
              "const x = new XMLHttpRequest(); x.open('POST', p, false); "
              "x.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded;charset=UTF-8'); "
              "x.setRequestHeader('X-Same-Domain', '1'); x.send(b); "
              "out.push({s: x.status, t: x.responseText, ms: Date.now() - t0}); } "
              "catch (e) { out.push({s: 0, t: '', e: String(e), ms: Date.now() - t0}); } } return out; })()"
              ) % json.dumps([list(c) for c in calls])
        res = self._run([command("run_js", js_code=js, out_type="list", timeout=min(timeout - 10, 170))],
                        timeout=timeout)
        step = res.steps[0] if res.steps else None
        if step is None or not step.success:
            err = f"{step.error_name}: {step.error_message}" if step else f"{res.status} {res.error_name}"
            return [HttpResult(p, 0, "", {}, error=err[:500]) for p, _ in calls]
        rows = (step.data or {}).get("result") if isinstance(step.data, dict) else None
        if not isinstance(rows, list) or len(rows) != len(calls):
            return [HttpResult(p, 0, "", {}, error=f"unexpected run_js result: {str(step.data)[:200]}") for p, _ in calls]
        out = [HttpResult(p, int(r.get("s") or 0), str(r.get("t") or ""), {}, elapsed=(r.get("ms") or 0) / 1000.0,
                          error=r.get("e")) for (p, _), r in zip(calls, rows)]
        if any(r.blocked for r in out):
            raise MapsBlocked("Google answered with a block page (429 / unusual traffic)")
        return out

    # ------------------------------------------------------------------- JS
    def js(self, code: str, *, out_type: str | None = "dict", timeout: float = 60.0) -> Any:
        res = self._run([command("run_js", js_code=code, out_type=out_type, timeout=min(timeout, 170))],
                        timeout=timeout + 30)
        step = res.steps[0] if res.steps else None
        if step is None or not step.success:
            raise RuntimeError(f"run_js failed: {step.error_name if step else res.status} "
                               f"{(step.error_message if step else '') or ''}"[:400])
        return (step.data or {}).get("result")


def is_html(text: str) -> bool:
    head = (text or "")[:200].lstrip().lower()
    return head.startswith("<!doctype") or head.startswith("<html")


def looks_like_fid(text: str) -> bool:
    return bool(re.match(r"^0x[0-9a-f]+:0x[0-9a-f]+$", text or ""))
