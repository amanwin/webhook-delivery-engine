"""Synchronous webhook delivery engine."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from .exceptions import ConfigurationError
from .models import WebhookEvent
from .transport import HTTPResponse, HTTPTransport, UrllibTransport, encode_json


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Outcome of one delivery attempt."""

    event_id: str
    endpoint: str
    status_code: int
    response_body: bytes = b""
    response_headers: dict[str, str] | None = None

    @property
    def delivered(self) -> bool:
        """Whether the endpoint returned a successful 2xx response."""
        return 200 <= self.status_code < 300


class WebhookDeliveryEngine:
    """Deliver webhook events with one HTTP POST per call.

    Retries, persistence, background workers and rate limiting are intentionally left
    for future modules. A non-2xx response is returned as a result, not raised.
    Transport failures raise :class:`TransportError`.
    """

    def __init__(self, *, timeout: float = 5.0, transport: HTTPTransport | None = None) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ConfigurationError("timeout must be a positive number")
        if timeout <= 0 or timeout == float("inf") or timeout != timeout:
            raise ConfigurationError("timeout must be a finite positive number")
        self._timeout = float(timeout)
        self._transport = transport if transport is not None else UrllibTransport()
        if not callable(getattr(self._transport, "post", None)):
            raise ConfigurationError("transport must provide a callable post method")

    def deliver(self, endpoint: str, event: WebhookEvent) -> DeliveryResult:
        """Send an event to ``endpoint`` once and return the response details."""
        self._validate_endpoint(endpoint)
        if not isinstance(event, WebhookEvent):
            raise TypeError("event must be a WebhookEvent")

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "webhook-delivery-engine/0.1.0",
            "X-Webhook-Event-Id": event.event_id,
            "X-Webhook-Event-Type": event.event_type,
        }
        headers.update(event.headers)
        response: HTTPResponse = self._transport.post(
            endpoint,
            encode_json(event.payload),
            headers,
            self._timeout,
        )
        return DeliveryResult(
            event_id=event.event_id,
            endpoint=endpoint,
            status_code=response.status_code,
            response_body=response.body,
            response_headers=response.headers,
        )

    @staticmethod
    def _validate_endpoint(endpoint: str) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ConfigurationError("endpoint must be a non-empty URL")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError("endpoint must be an absolute HTTP or HTTPS URL")
