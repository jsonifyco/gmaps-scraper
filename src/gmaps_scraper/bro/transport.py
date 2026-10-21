"""bro transport over the public ``bro-api-sdk``.

One :class:`BroBrowser` wraps one bro session (one VM).  The SDK runs the
commands of a session strictly one after another, so the scraper gives every
browser to a single worker and gets parallelism from several sessions.

Discipline (from the plan and the neighbouring Instagram scraper's lessons):

* session creation is bounded: the SDK's ``create_session`` waits for ``idle``
  without limit, so it runs on a thread; past the bound the booting session
  is found in the account's list and stopped;
* a batch is submitted with ``await_completion=False`` and then observed by
  its command id; a lost submit response is reconciled through the session's
  command list before anything is sent again;
* a command that does not settle in time is never silently resent: the
  session's own status is read and :class:`BroOutcomeUnknown` is raised, the
  caller replaces the browser;
* steps whose data was offloaded are downloaded from ``offloaded_data_url``;
* ``close`` stops the session and waits for the final billing sync.

Only public SDK calls are used; no admin endpoint is touched.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import bro as sdk
import requests
from bro.exceptions import BroAPIError

log = logging.getLogger(__name__)

TERMINAL_COMMAND = {"done", "failed", "stopped", "cancelled"}
DEAD_SESSION = {"terminated", "stopped", "failed", "cancelled"}
BOOTING_SESSION = {"queued", "initializing"}
#: getbro sits behind a WAF; a browser-like agent is what has been proven to pass
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 gmaps-scraper-bro")


class BroError(Exception):
    """Base class of transport failures."""

    def __init__(self, message: str, *, session_id: str | None = None, command_id: str | None = None):
        super().__init__(message)
        self.session_id = session_id
        self.command_id = command_id


class BroAuthError(BroError):
    """The API key was rejected or the balance is exhausted (not retryable)."""


class BroSessionDead(BroError):
    """The session ended (timeout, stop, failure) and accepts no more commands."""


class BroCreateFailed(BroError):
    """A session could not be created or did not become idle in time."""


class BroOutcomeUnknown(BroError):
    """A command was (or may have been) accepted but its result is unknown."""


class BroCommandLost(BroOutcomeUnknown):
    """The session is idle with no current command, yet ours never settled.

    The VM may be killed for inactivity, so the session must be replaced."""


@dataclass
class SessionOptions:
    enable_proxy: bool = False
    proxy_tier: str | None = None
    proxy_policy: str | None = None
    country: str | None = None
    city: str | None = None
    block_unproxied: bool = False
    session_timeout: float = 21600.0
    #: what the session is kept for (not sent to bro): sessions of another purpose are not reused
    purpose: str = ""

    def sdk_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"session_timeout": float(self.session_timeout)}
        if self.enable_proxy:
            kwargs["enable_proxy"] = True
            if self.proxy_tier:
                kwargs["proxy_tier"] = self.proxy_tier
            if self.proxy_policy:
                kwargs["proxy_policy"] = self.proxy_policy
            if self.block_unproxied:
                kwargs["block_unproxied"] = True
        if self.country:
            kwargs["country"] = self.country
        if self.city:
            kwargs["city"] = self.city
        return kwargs

    def describe(self) -> str:
        tag = f" [{self.purpose}]" if self.purpose else ""
        if not self.enable_proxy:
            return "no-proxy" + (f" {self.country}" if self.country else "") + tag
        return f"proxy {self.proxy_tier or 'lite'}/{self.proxy_policy or 'basic'} {self.country or '-'}/{self.city or '-'}{tag}"


@dataclass
class StepResult:
    index: int
    command: str
    success: bool
    data: Any = None
    error_name: str | None = None
    error_message: str | None = None
    offloaded: bool = False
    duration: float | None = None


@dataclass
class CommandResult:
    command_id: str
    status: str
    steps: list[StepResult]
    error_name: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    finished_at: str | None = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "done" and all(s.success for s in self.steps)

    def step(self, index: int) -> StepResult:
        return self.steps[index]


def compact_js(code: str) -> str:
    """One-line form of a JS snippet.

    bro's ``run_js`` wrapper mis-handles multi-line code whose last line starts
    with ``return`` (``SyntaxError: Unexpected token ':'`` on 2026-10-01), so
    every snippet is sent on one line.  Snippets must therefore not use ``//``
    line comments.
    """
    return " ".join(line.strip() for line in code.strip().splitlines() if line.strip())


def command(name: str, **params: Any) -> dict[str, Any]:
    """A command dict (``{"command": name, "params": {...}}``) without ``None`` params."""
    if name == "run_js" and isinstance(params.get("js_code"), str):
        params["js_code"] = compact_js(params["js_code"])
    return {"command": name, "params": {k: v for k, v in params.items() if v is not None}}


def _parse_ts(raw: Any) -> float | None:
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _api_status(exc: BroAPIError) -> int | None:
    text = str(exc)
    if text.startswith("API Error (") and ")" in text:
        try:
            return int(text[len("API Error ("):text.index(")")])
        except ValueError:
            return None
    return None


class BroTransport:
    """Account-level operations and browser factory."""

    CREATE_TIMEOUT = 300.0
    #: pause between list snapshots when a submit answer was lost
    RECONCILE_PAUSE = 2.0

    def __init__(self, api_key: str, *, base_url: str = "https://api.getbro.ws",
                 on_event: Callable[[str, dict[str, Any]], None] | None = None,
                 client: Any | None = None):
        if not api_key:
            raise BroAuthError("BRO_API_KEY is not set")
        self.client = client or sdk.BroClient(api_key, base_url=base_url, timeout=30.0)
        if client is None:
            self.client.headers.setdefault("User-Agent", USER_AGENT)
        self.on_event = on_event or (lambda kind, data: None)
        self._creates_lock = threading.Lock()
        self._creates_in_flight = 0

    # ----------------------------------------------------------------- account
    def account(self) -> dict[str, Any]:
        try:
            return dict(self.client.get_user() or {})
        except BroAPIError as exc:
            status = _api_status(exc)
            if status in (401, 403):
                raise BroAuthError(f"bro rejected the API key: {exc}") from exc
            raise BroError(f"could not read the bro account: {exc}") from exc

    # ---------------------------------------------------------------- sessions
    def open_browser(self, options: SessionOptions, *, create_timeout: float | None = None,
                     label: str = "") -> "BroBrowser":
        bound = float(create_timeout or self.CREATE_TIMEOUT)
        started = time.time()
        box: dict[str, Any] = {}
        done = threading.Event()
        abandoned = threading.Event()

        def boot() -> None:
            try:
                box["session"] = self.client.create_session(**options.sdk_kwargs())
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
                box["error"] = exc
            finally:
                with self._creates_lock:
                    self._creates_in_flight -= 1
                done.set()
                if abandoned.is_set() and box.get("session") is not None:
                    self._stop_quietly(box["session"].session_id, "booted after the create bound")

        with self._creates_lock:
            self._creates_in_flight += 1
        threading.Thread(target=boot, name="bro-create", daemon=True).start()
        if not done.wait(bound):
            abandoned.set()
            stranded = self._stop_stranded(started)
            raise BroCreateFailed(f"session did not become idle within {bound:.0f}s"
                                  + (f" (stopped {stranded[:8]})" if stranded else ""), session_id=stranded or None)
        if "error" in box:
            exc = box["error"]
            if isinstance(exc, BroAPIError):
                status = _api_status(exc)
                if status in (401, 403):
                    raise BroAuthError(f"bro rejected the API key: {exc}") from exc
                if status == 402:
                    raise BroAuthError(f"bro balance exhausted: {exc}") from exc
                raise BroCreateFailed(f"session create failed: {exc}") from exc
            if isinstance(exc, requests.RequestException):
                raise BroCreateFailed(f"session create transport error: {exc}") from exc
            raise exc
        session = box["session"]
        browser = BroBrowser(self, session, options, created_wall=started, label=label)
        self.on_event("session_created", {"session_id": session.session_id, "options": options.describe(),
                                          "boot_seconds": round(time.time() - started, 1),
                                          "browser": session.metadata.get("browser")})
        return browser

    def _stop_stranded(self, started: float) -> str:
        """Stop the single booting session created by this create, if provable."""
        try:
            listed = self.client.list_sessions(limit=20) or {}
        except (BroAPIError, requests.RequestException) as exc:
            log.warning("could not list sessions to stop a stranded boot: %s", exc)
            return ""
        candidates = []
        for entry in listed.get("sessions") or []:
            if str(entry.get("status")) not in BOOTING_SESSION:
                continue
            created = _parse_ts(entry.get("created_at"))
            if created is not None and created < started - 10:
                continue
            candidates.append(str(entry.get("session_id")))
        with self._creates_lock:
            others = self._creates_in_flight
        if len(candidates) == 1 and others == 0:
            self._stop_quietly(candidates[0], "still booting past the create bound")
            return candidates[0]
        if candidates:
            log.warning("%d booting session(s) found, none provably ours: %s", len(candidates),
                        ", ".join(c[:8] for c in candidates))
        return ""

    def _stop_quietly(self, session_id: str, reason: str) -> None:
        try:
            self.client.stop_session(session_id)
            log.warning("stopped session %s: %s", session_id[:8], reason)
        except (BroAPIError, requests.RequestException) as exc:
            log.warning("could not stop session %s (%s): %s", session_id[:8], reason, exc)

    def session_info(self, session_id: str) -> dict[str, Any]:
        session = self.client.get_session(session_id)
        return dict(session.metadata or {})

    def stop_and_bill(self, session_id: str, *, wait: float = 30.0) -> dict[str, Any]:
        """Stop a session (idempotent) and return its last known metadata after billing sync."""
        try:
            self.client.stop_session(session_id)
        except (BroAPIError, requests.RequestException) as exc:
            log.warning("stop of %s failed: %s", session_id[:8], exc)
        return self._final_info(session_id, wait=wait)

    def _final_info(self, session_id: str, *, wait: float) -> dict[str, Any]:
        deadline = time.monotonic() + wait
        info: dict[str, Any] = {}
        stopped_at = time.time()
        while True:
            try:
                info = self.session_info(session_id)
            except (BroAPIError, requests.RequestException) as exc:
                log.debug("billing read of %s failed: %s", session_id[:8], exc)
            status = str(info.get("status") or "")
            synced = _parse_ts((info.get("billing") or {}).get("last_synced_at"))
            if status in DEAD_SESSION and synced is not None and synced >= stopped_at - 5:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(2.0)
        return info


class BroBrowser:
    """One live bro session, used by one worker at a time."""

    SUBMIT_RETRIES = 2
    #: margin kept before the session's own timeout when deciding to rotate
    ROTATE_MARGIN = 240.0

    def __init__(self, transport: BroTransport, session: Any, options: SessionOptions, *,
                 created_wall: float, label: str = ""):
        self.transport = transport
        self.session = session
        self.options = options
        self.session_id: str = session.session_id
        self.created_wall = created_wall
        self.created_mono = time.monotonic()
        self.label = label
        self.closed = False
        self.final_info: dict[str, Any] | None = None
        self.commands_sent = 0
        self.busy_seconds = 0.0
        #: url the page was last seen at (updated by callers that navigate)
        self.current_url: str | None = None
        self._known_commands: set[str] = set()

    # ------------------------------------------------------------- lifetime
    @property
    def age(self) -> float:
        return time.monotonic() - self.created_mono

    @property
    def remaining(self) -> float:
        return self.options.session_timeout - self.age

    def should_rotate(self, upcoming: float = 0.0) -> bool:
        return self.remaining - upcoming < self.ROTATE_MARGIN

    def close(self) -> dict[str, Any]:
        if self.closed:
            return self.final_info or {}
        self.closed = True
        self.final_info = self.transport.stop_and_bill(self.session_id)
        billing = (self.final_info or {}).get("billing") or {}
        self.transport.on_event("session_closed", {"session_id": self.session_id,
                                                   "status": self.final_info.get("status"),
                                                   "billing": billing,
                                                   "proxy_usage": self.final_info.get("proxy_usage"),
                                                   "commands": self.commands_sent})
        return self.final_info

    # ------------------------------------------------------------- commands
    def execute(self, commands: list[dict[str, Any]], *, timeout: float = 180.0,
                repeatable: bool = True) -> CommandResult:
        """Run a batch and return its steps (offloaded data already downloaded).

        ``repeatable`` batches (reads, navigation, scrolling) may be sent again
        when a submit answer was lost and the command list proves the first
        send was not accepted; anything else stays unknown.
        """
        if self.closed:
            raise BroSessionDead("browser already closed", session_id=self.session_id)
        payloads = [sdk.CommandPayload(command=c["command"], params=c.get("params") or None) for c in commands]
        started = time.time()
        command_id = self._submit(payloads, commands, repeatable=repeatable, started=started)
        data = self._observe(command_id, timeout=timeout, started=started)
        steps = []
        for raw in list((data.get("response") or {}).get("commands") or []):
            step = StepResult(index=int(raw.get("step") or len(steps)), command=str(raw.get("command") or ""),
                              success=bool(raw.get("success")), data=raw.get("data"),
                              error_name=raw.get("error_name"), error_message=raw.get("error_message"),
                              duration=raw.get("duration"))
            url = raw.get("offloaded_data_url")
            if url and step.data is None:
                step.data = self._download(url)
                step.offloaded = True
            steps.append(step)
        elapsed = time.time() - started
        self.busy_seconds += elapsed
        result = CommandResult(command_id=command_id, status=str(data.get("status") or ""), steps=steps,
                               error_name=data.get("error_name"), error_message=data.get("error_message"),
                               created_at=data.get("created_at"), finished_at=data.get("finished_at"),
                               elapsed=elapsed)
        if result.status in ("stopped", "cancelled"):
            raise BroSessionDead(f"command {result.status}: {result.error_name} {result.error_message}",
                                 session_id=self.session_id, command_id=command_id)
        return result

    def _submit(self, payloads: list[Any], commands: list[dict[str, Any]], *, repeatable: bool,
                started: float) -> str:
        attempts = 0
        while True:
            attempts += 1
            try:
                answer = self.session.execute(payloads, await_completion=False)
                command_id = str((answer or {}).get("command_id") or "")
                if not command_id:
                    raise BroOutcomeUnknown("submit accepted without a command id", session_id=self.session_id)
                self._known_commands.add(command_id)
                self.commands_sent += 1
                return command_id
            except BroAPIError as exc:
                status = _api_status(exc)
                if status in (401, 403):
                    raise BroAuthError(f"bro rejected the API key: {exc}", session_id=self.session_id) from exc
                if status == 402:
                    raise BroAuthError(f"bro balance exhausted: {exc}", session_id=self.session_id) from exc
                if status in (404, 409, 410) or (status == 400 and "not active" in str(exc).lower()):
                    raise BroSessionDead(f"session refused the command: {exc}", session_id=self.session_id) from exc
                if status is not None and status < 500:
                    raise BroError(f"command rejected: {exc}", session_id=self.session_id) from exc
                fault: Exception = exc
            except requests.RequestException as exc:
                fault = exc
            # The answer is lost (transport error or 5xx): was the batch accepted?
            adopted = self._reconcile(started, commands[0]["command"] if commands else "")
            if adopted:
                return adopted
            if not repeatable or attempts > self.SUBMIT_RETRIES:
                raise BroOutcomeUnknown(f"submit answer lost and not provably rejected: {fault}",
                                        session_id=self.session_id) from fault
            log.warning("[%s] submit lost (%s); command list shows nothing new, resending a repeatable batch",
                        self.session_id[:8], fault)
            time.sleep(1.5 * attempts)

    def _reconcile(self, started: float, first_name: str) -> str | None:
        """The command the lost submit created, or None when the history proves there is none.

        A history that cannot be read proves nothing: that is ``BroOutcomeUnknown``, never a
        licence to resend (the first batch may be running and may consume page buffers)."""
        readable = False
        for snapshot in range(3):
            if snapshot:
                time.sleep(BroTransport.RECONCILE_PAUSE)
            try:
                listed = self.session.get_commands(limit=20) or {}
            except (BroAPIError, requests.RequestException):
                continue
            readable = True
            found: dict[str, set[str]] = {}
            for entry in listed.get("commands") or []:
                cid = str(entry.get("command_id") or "")
                if not cid or cid in self._known_commands:
                    continue
                created = _parse_ts(entry.get("created_at"))
                if created is not None and created < started - 5:
                    continue
                found.setdefault(cid, set()).add(str(entry.get("command") or ""))
            if len(found) == 1:
                cid, names = next(iter(found.items()))
                if not first_name or first_name in names or not names - {""}:
                    self._known_commands.add(cid)
                    self.commands_sent += 1
                    log.warning("[%s] lost submit adopted as command %s", self.session_id[:8], cid[:8])
                    return cid
            if len(found) > 1:
                raise BroOutcomeUnknown(f"lost submit is ambiguous: {len(found)} new commands listed",
                                        session_id=self.session_id)
        if not readable:
            raise BroOutcomeUnknown("submit answer lost and the command history is unreadable",
                                    session_id=self.session_id)
        return None

    #: seconds after which an unsettled command is cross-checked with the session
    LOST_CHECK_AFTER = 45.0
    #: consecutive "session idle, not running ours" sightings that mean lost
    LOST_SIGHTINGS = 3

    def _observe(self, command_id: str, *, timeout: float, started: float) -> dict[str, Any]:
        deadline = started + timeout
        failures = 0
        idle_sightings = 0
        next_check = started + self.LOST_CHECK_AFTER
        while True:
            try:
                data = dict(self.session.get_command(command_id) or {})
                failures = 0
                if str(data.get("status")) in TERMINAL_COMMAND:
                    return data
                if time.time() >= next_check:
                    next_check = time.time() + 10.0
                    info = self.session.get_info() or {}
                    status = str(info.get("status") or "")
                    if status in DEAD_SESSION:
                        raise BroSessionDead(
                            f"session is {status} ({info.get('error_name')}: {info.get('error_message')}) "
                            f"while command {command_id[:8]} was {data.get('status')}",
                            session_id=self.session_id, command_id=command_id)
                    if status == "idle" and not info.get("command_id"):
                        idle_sightings += 1
                        if idle_sightings >= self.LOST_SIGHTINGS:
                            raise BroCommandLost(
                                f"command {command_id[:8]} is {data.get('status')} but the session is idle",
                                session_id=self.session_id, command_id=command_id)
                    else:
                        idle_sightings = 0
            except BroAPIError as exc:
                status = _api_status(exc)
                if status in (401, 403):
                    raise BroAuthError(str(exc), session_id=self.session_id, command_id=command_id) from exc
                failures += 1
                log.debug("[%s] get_command failed: %s", self.session_id[:8], exc)
            except requests.RequestException as exc:
                failures += 1
                log.debug("[%s] get_command transport error: %s", self.session_id[:8], exc)
            if time.time() >= deadline:
                break
            time.sleep(min(0.5 + 0.5 * failures, 5.0))
        # Not settled in time: is the session itself still alive?
        status = ""
        try:
            info = self.session.get_info() or {}
            status = str(info.get("status") or "")
        except (BroAPIError, requests.RequestException):
            pass
        if status in DEAD_SESSION:
            raise BroSessionDead(f"session is {status} while command {command_id[:8]} was pending",
                                 session_id=self.session_id, command_id=command_id)
        raise BroOutcomeUnknown(f"command {command_id[:8]} not settled within {timeout:.0f}s (session {status or '?'})",
                                session_id=self.session_id, command_id=command_id)

    #: an offloaded result is downloaded within this many seconds in all (bro closes a session
    #: after ~300 s without commands, and the session is idle while we download) ...
    DOWNLOAD_DEADLINE = 120.0
    #: ... and up to this size; a larger payload (a HAR log of hours) is refused, not parsed
    DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024

    @classmethod
    def _download(cls, url: str) -> Any:
        """Fetch an offloaded command result within a total deadline and a size cap.

        The transfer runs on a helper thread and the caller waits for it only until the
        deadline: a server that trickles bytes (never idle long enough for the read timeout)
        cannot hold the caller past it.  On the deadline the response is closed (on another thread) so the
        helper's blocked read ends too; the helper exits at its next step in any case."""
        deadline = time.monotonic() + cls.DOWNLOAD_DEADLINE
        box: dict[str, Any] = {}
        cancel = threading.Event()

        def fetch() -> None:
            try:
                box["value"] = cls._download_until(url, deadline, cancel, box)
            except BaseException as exc:  # noqa: BLE001 - handed to the caller
                box["error"] = exc

        worker = threading.Thread(target=fetch, name="bro-download", daemon=True)
        worker.start()
        worker.join(max(0.0, deadline - time.monotonic()))
        if worker.is_alive():
            cancel.set()
            resp = box.get("resp")
            if resp is not None:
                # closing a response another thread is reading can block on its lock: do it off
                # the caller's thread
                threading.Thread(target=cls._close_quietly, args=(resp,), daemon=True).start()
            raise BroOutcomeUnknown(f"offloaded payload not downloaded within {cls.DOWNLOAD_DEADLINE:.0f}s "
                                    f"({box.get('got', 0)} bytes so far)")
        if "error" in box:
            raise box["error"]
        return box["value"]

    @staticmethod
    def _close_quietly(resp: Any) -> None:
        try:
            resp.close()
        except Exception:  # noqa: BLE001 - best effort, the caller has already moved on
            pass

    @classmethod
    def _download_until(cls, url: str, deadline: float, cancel: threading.Event, box: dict) -> Any:
        last: Exception | None = None
        attempt = 0
        while not cancel.is_set() and time.monotonic() < deadline:
            attempt += 1
            left = max(0.5, deadline - time.monotonic())
            try:
                with requests.get(url, timeout=(min(10.0, left), min(30.0, left)), stream=True) as resp:
                    box["resp"] = resp
                    if resp.status_code != 200:
                        last = RuntimeError(f"HTTP {resp.status_code}")
                    else:
                        size = int(resp.headers.get("Content-Length") or 0)
                        if size > cls.DOWNLOAD_MAX_BYTES:
                            raise BroOutcomeUnknown(f"offloaded payload of {size} bytes exceeds the "
                                                    f"{cls.DOWNLOAD_MAX_BYTES} byte limit")
                        chunks, got, logged = [], 0, 0
                        for chunk in resp.iter_content(1 << 16):
                            if cancel.is_set():
                                return None
                            chunks.append(chunk)
                            got += len(chunk)
                            box["got"] = got
                            if got > cls.DOWNLOAD_MAX_BYTES:
                                raise BroOutcomeUnknown(f"offloaded payload exceeds {cls.DOWNLOAD_MAX_BYTES} bytes")
                            if got - logged >= 8 << 20:
                                log.info("downloading offloaded payload: %d MB", got >> 20)
                                logged = got
                        text = b"".join(chunks).decode("utf-8", errors="replace")
                        try:
                            return json.loads(text)
                        except ValueError:
                            return text
            except requests.RequestException as exc:
                last = exc
            except (AttributeError, OSError, ValueError) as exc:  # a read cut by the caller's close
                if cancel.is_set():
                    return None
                last = exc
            cancel.wait(min(1.0 + attempt * 2, max(0.0, deadline - time.monotonic())))
        raise BroOutcomeUnknown(f"offloaded payload could not be downloaded within "
                                f"{cls.DOWNLOAD_DEADLINE:.0f}s: {last}")

    def info(self) -> dict[str, Any]:
        try:
            return dict(self.session.get_info() or {})
        except (BroAPIError, requests.RequestException) as exc:
            raise BroError(f"session info failed: {exc}", session_id=self.session_id) from exc
