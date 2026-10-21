"""Decoding of Google's guarded JSON responses.

* plain endpoints answer ``)]}'\\n<json>``;
* ``batchexecute`` with ``rt=c`` answers ``)]}'\\n\\n<len>\\n<json-array>\\n<len>\\n...``
  where each array holds envelopes such as
  ``["wrb.fr", "<rpc id>", "<payload json string>", null, null, <error>, "generic"]``.

A missing payload with an error slot (``[3]``) or a payload such as
``[null, null, null, null, null, true, [true]]`` is a refusal, not an empty
result; callers decide which shapes count as "end of list".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

XSSI_PREFIX = ")]}'"


class ResponseFormatError(ValueError):
    """The body is not the guarded JSON the endpoint should return."""


def strip_xssi(text: str) -> str:
    if text is None:
        raise ResponseFormatError("empty response body")
    stripped = text.lstrip()
    if stripped.startswith(XSSI_PREFIX):
        return stripped[len(XSSI_PREFIX):]
    return stripped


def loads_guarded(text: str) -> Any:
    """Parse a ``)]}'``-guarded JSON body."""
    body = strip_xssi(text).strip()
    if not body:
        raise ResponseFormatError("empty JSON body")
    if body[0] not in "[{":
        raise ResponseFormatError(f"not JSON: {body[:80]!r}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ResponseFormatError(f"invalid JSON: {exc}") from exc


@dataclass
class RpcEnvelope:
    rpc: str
    payload: Any            # decoded payload, or None
    raw_payload: str | None
    error: Any              # the error slot (e.g. [3]) or None


def parse_batchexecute(text: str) -> list[RpcEnvelope]:
    """All ``wrb.fr`` envelopes of a chunked ``batchexecute`` body."""
    body = strip_xssi(text)
    decoder = json.JSONDecoder()
    envelopes: list[RpcEnvelope] = []
    pos, n = 0, len(body)
    while pos < n:
        ch = body[pos]
        if ch in " \t\r\n" or ch.isdigit():
            pos += 1
            continue
        if ch != "[":
            raise ResponseFormatError(f"unexpected batchexecute chunk at {pos}: {body[pos:pos + 40]!r}")
        try:
            chunk, end = decoder.raw_decode(body, pos)
        except json.JSONDecodeError as exc:
            raise ResponseFormatError(f"invalid batchexecute chunk: {exc}") from exc
        pos = end
        for entry in chunk if isinstance(chunk, list) else []:
            if isinstance(entry, list) and len(entry) >= 3 and entry[0] == "wrb.fr":
                raw = entry[2]
                payload = None
                if isinstance(raw, str) and raw:
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise ResponseFormatError(f"invalid {entry[1]} payload: {exc}") from exc
                error = entry[5] if len(entry) > 5 else None
                envelopes.append(RpcEnvelope(str(entry[1]), payload, raw if isinstance(raw, str) else None, error))
    return envelopes


def rpc_payload(text: str, rpc: str) -> RpcEnvelope:
    for env in parse_batchexecute(text):
        if env.rpc == rpc:
            return env
    raise ResponseFormatError(f"no {rpc} envelope in response")


def dig(obj: Any, *path: int | str, default: Any = None) -> Any:
    """Safe nested index: ``dig(a, 4, 7)`` is ``a[4][7]`` or ``default``."""
    cur = obj
    for key in path:
        if isinstance(cur, str) or (isinstance(key, int) and not isinstance(cur, list)):
            return default
        try:
            cur = cur[key]
        except (IndexError, KeyError, TypeError):
            return default
        if cur is None:
            return default
    return cur
