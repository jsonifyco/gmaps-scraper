"""Bounded, process-local cache of validated discovery responses."""
from __future__ import annotations

import gzip
import threading
from collections import OrderedDict
from dataclasses import replace

from ..maps.client import HttpResult


class SearchResponseCache:
    def __init__(self, max_bytes: int = 64 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.size = 0
        self.rows: OrderedDict[tuple[str, str, str], tuple[HttpResult, bytes]] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, url: str, hl: str, gl: str) -> HttpResult | None:
        with self.lock:
            row = self.rows.get((url, hl, gl))
            if row is None:
                return None
            response, body = row
            self.rows.move_to_end((url, hl, gl))
        return replace(response, text=gzip.decompress(body).decode('utf-8'), headers=dict(response.headers))

    def put(self, response: HttpResult, hl: str, gl: str) -> None:
        # Caller must have successfully parsed the complete response first.
        if response.status != 200 or response.error or not response.text:
            return
        body = gzip.compress(response.text.encode('utf-8'))
        if len(body) > self.max_bytes:
            return
        key = (response.url, hl, gl)
        with self.lock:
            old = self.rows.pop(key, None)
            if old:
                self.size -= len(old[1])
            while self.rows and self.size + len(body) > self.max_bytes:
                _, removed = self.rows.popitem(last=False)
                self.size -= len(removed[1])
            self.rows[key] = (replace(response, text='', headers=dict(response.headers)), body)
            self.size += len(body)
