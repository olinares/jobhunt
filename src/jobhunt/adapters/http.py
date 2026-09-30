"""Polite shared HTTP client for talking to ATS endpoints.

Every adapter goes through ``PoliteClient`` so the whole pipeline follows the same rules:
a descriptive User-Agent, a minimum interval between requests to the same host, and
retries with exponential backoff on 429, 5xx and transient network failures.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any, Self

import httpx

DEFAULT_USER_AGENT = "jobhunt/0.1 (+https://github.com/olinares/jobhunt)"

_MAX_RETRY_AFTER = 60.0  # never let a server park us for longer than this per attempt

# Transient transport failures worth retrying: timeouts (connect/read/write/pool), network
# errors (connect, read, write, close) and the remote end dropping or garbling the
# connection. Deliberately excludes permanent problems such as InvalidURL,
# UnsupportedProtocol, ProxyError and TooManyRedirects.
_RETRYABLE_EXCEPTIONS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


class PoliteClient:
    """A small synchronous wrapper around ``httpx.Client``.

    - Waits at least ``min_interval`` seconds between requests to the same host.
    - Retries 429/5xx up to ``max_retries`` times, sleeping ``backoff * 2**attempt``
      seconds, or the server's ``Retry-After`` when it sends one.
    - Retries transient transport failures (``httpx.TimeoutException``,
      ``httpx.NetworkError`` such as connect and read errors, and
      ``httpx.RemoteProtocolError``) with the same attempt count and backoff.
    - Raises ``httpx.HTTPStatusError`` for any other non-2xx response, and for a
      retryable status once retries are exhausted. Once retries are exhausted on a
      transport failure, the original exception is re-raised.

    ``transport`` and ``sleep`` exist so tests can run without network or real delays.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        min_interval: float = 1.0,
        max_retries: int = 4,
        backoff: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff = backoff
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            transport=transport,
        )

    def get_json(self, url: str, params: dict | None = None) -> Any:
        return self._request("GET", url, params=params).json()

    def post_json(self, url: str, payload: Any) -> Any:
        return self._request("POST", url, json=payload).json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = httpx.URL(url).host
        attempt = 0
        while True:
            self._wait_for_host(host)
            try:
                response = self._client.request(method, url, **kwargs)
            except _RETRYABLE_EXCEPTIONS:
                # A failed attempt still counts against the host's polite interval.
                self._last_request[host] = time.monotonic()
                if attempt >= self.max_retries:
                    raise
                self._sleep(self.backoff * (2**attempt))
                attempt += 1
                continue
            self._last_request[host] = time.monotonic()
            if _is_retryable(response.status_code) and attempt < self.max_retries:
                self._sleep(self._retry_delay(response, attempt))
                attempt += 1
                continue
            response.raise_for_status()
            return response

    def _wait_for_host(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is None or self.min_interval <= 0:
            return
        remaining = self.min_interval - (time.monotonic() - last)
        if remaining > 0:
            self._sleep(remaining)

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is not None:
            return min(retry_after, _MAX_RETRY_AFTER)
        return self.backoff * (2**attempt)


def _is_retryable(status: int) -> bool:
    return status == 429 or 500 <= status < 600


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header given either as seconds or as an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())
