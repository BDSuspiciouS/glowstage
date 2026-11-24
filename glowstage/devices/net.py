"""Tiny HTTP/JSON helper (stdlib only) used by the network light drivers."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class HttpError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(f"HTTP {status} {message}".strip())
        self.status = status


def http_json(method: str, url: str, payload=None, timeout: float = 3.0):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.reason) from None
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None
