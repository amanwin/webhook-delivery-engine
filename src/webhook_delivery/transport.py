"""HTTP transport abstraction and standard-library implementation."""

from __future__ import annotations

import json
from dataclasses import dataclass
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
            # HTTP errors are still HTTP responses; callers can inspect their status.
            return HTTPResponse(
                status_code=error.code,
                body=error.read(),
                headers=dict(error.headers.items()) if error.headers else {},
            )
        except (URLError, TimeoutError, OSError) as error:
            raise TransportError(f"HTTP request failed: {error}") from error


def encode_json(payload: dict[str, object]) -> bytes:
    """Serialize a JSON-compatible payload to UTF-8 bytes."""
    try:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(f"payload is not JSON serializable: {error}") from error
