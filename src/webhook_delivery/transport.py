"""HTTP transport abstraction and standard-library implementation."""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from http.client import HTTPException, IncompleteRead
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .exceptions import TransportError


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    """A minimal HTTP response returned by a transport."""

    status_code: int
    body: bytes = b""
    headers: dict[str, str] | None = None


class HTTPTransport(Protocol):
    """Interface implemented by HTTP transports used by the engine."""

    def post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> HTTPResponse:
        """Send a POST request and return its HTTP response."""


class UrllibTransport:
    """HTTP transport implemented with Python's standard library."""

    def post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> HTTPResponse:
        request = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                return HTTPResponse(
                    status_code=response.status,
                    body=response.read(),
                    headers=dict(response.headers.items()),
                )
        except HTTPError as error:
            # 4xx/5xx are still HTTP responses: the status is what callers and the retry
            # policy act on, so report it even if the body cannot be read completely.
            return HTTPResponse(
                status_code=error.code,
                body=_read_body_best_effort(error),
                headers=dict(error.headers.items()) if error.headers else {},
            )
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            # URLError: DNS, connection refused, and timeouts/TLS failures during connect.
            # OSError: connection reset or closed before any response (RemoteDisconnected).
            # HTTPException: malformed status line, truncated body (BadStatusLine,
            # IncompleteRead). All are transport failures; only TLS ones are permanent.
            raise TransportError(
                f"HTTP request failed: {error}", retryable=not _is_tls_failure(error)
            ) from error


def _read_body_best_effort(error: HTTPError) -> bytes:
    try:
        return error.read()
    except IncompleteRead as partial:
        return partial.partial
    except (OSError, HTTPException):
        return b""


def _is_tls_failure(error: BaseException) -> bool:
    """Whether ``error`` is a TLS handshake or certificate problem.

    ``urllib`` usually wraps these in ``URLError`` with the ``ssl`` error as ``reason``.
    Such failures are deterministic, so retrying them only delays surfacing the problem.
    """
    return isinstance(error, ssl.SSLError) or isinstance(
        getattr(error, "reason", None), ssl.SSLError
    )


def encode_json(payload: dict[str, object]) -> bytes:
    """Serialize a JSON-compatible payload to UTF-8 bytes."""
    try:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(f"payload is not JSON serializable: {error}") from error
