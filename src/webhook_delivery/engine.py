"""Synchronous webhook delivery engine."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from .exceptions import ConfigurationError, TransportError
from .models import WebhookEvent
from .retry import RetryPolicy
from .transport import HTTPResponse, HTTPTransport, UrllibTransport, encode_json


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Outcome of a delivery, after any retries."""

    event_id: str
    endpoint: str
    status_code: int
    response_body: bytes = b""
    response_headers: dict[str, str] | None = None
    attempts: int = 1
    """Number of HTTP requests made, including the one that produced this result."""

    @property
    def delivered(self) -> bool:
        """Whether the endpoint returned a successful 2xx response."""
        return 200 <= self.status_code < 300


class WebhookDeliveryEngine:
    """Deliver webhook events with an HTTP POST, optionally retrying failed attempts.

    Without a :class:`RetryPolicy` the engine makes exactly one request. With one, it
    repeats the request while the policy says the outcome is retryable and attempts remain.
    In either case a non-2xx response is returned as a result, not raised, and a transport
    failure that is not (or can no longer be) retried raises :class:`TransportError`.

    Every attempt re-sends the same ``X-Webhook-Event-Id``; receivers should treat it as an
    idempotency key, because an attempt that times out may still have been processed.

    Persistence, background workers and rate limiting are left for future modules.
    """

    def __init__(
        self,
        *,
        timeout: float = 5.0,
        transport: HTTPTransport | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ConfigurationError("timeout must be a positive number")
        if timeout <= 0 or not math.isfinite(timeout):
            raise ConfigurationError("timeout must be a finite positive number")
        self._timeout = float(timeout)
        self._transport = transport if transport is not None else UrllibTransport()
        if not callable(getattr(self._transport, "post", None)):
            raise ConfigurationError("transport must provide a callable post method")
        if retry is not None and not isinstance(retry, RetryPolicy):
            raise ConfigurationError("retry must be a RetryPolicy")
        self._retry = retry if retry is not None else RetryPolicy()
        if not callable(sleep):
            raise ConfigurationError("sleep must be callable")
        self._sleep = sleep

    @property
    def retry_policy(self) -> RetryPolicy:
        """The policy in effect; a single-attempt policy when none was configured."""
        return self._retry

    def deliver(self, endpoint: str, event: WebhookEvent) -> DeliveryResult:
        """Send an event to ``endpoint`` and return the final response details.

        Raises :class:`TransportError` if the last attempt failed at the transport layer.
        """
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
        # Encode once, outside the loop: a serialization failure is deterministic and
        # must surface before any request is made, exactly as it did without retries.
        body = encode_json(event.payload)

        policy = self._retry
        attempt = 0
        while True:
            attempt += 1
            attempts_remain = attempt < policy.max_attempts
            try:
                response: HTTPResponse = self._transport.post(
                    endpoint, body, headers, self._timeout
                )
            except TransportError as error:
                if not attempts_remain or not policy.should_retry_error(error):
                    raise
                self._wait(policy.delay_before_retry(attempt))
                continue

            if not attempts_remain or not policy.should_retry_response(response):
                return DeliveryResult(
                    event_id=event.event_id,
                    endpoint=endpoint,
                    status_code=response.status_code,
                    response_body=response.body,
                    response_headers=response.headers,
                    attempts=attempt,
                )
            self._wait(policy.delay_before_retry(attempt, response))

    def _wait(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(seconds)

    @staticmethod
    def _validate_endpoint(endpoint: str) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ConfigurationError("endpoint must be a non-empty URL")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError("endpoint must be an absolute HTTP or HTTPS URL")
