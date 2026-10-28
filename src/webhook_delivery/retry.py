"""Retry policy for webhook delivery attempts."""

from __future__ import annotations

import math
import random as _random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime

from .exceptions import ConfigurationError, TransportError
from .transport import HTTPResponse

#: HTTP statuses that usually indicate a transient condition on the receiving side.
#:
#: 408 Request Timeout, 425 Too Early and 429 Too Many Requests are explicit "try again
#: later" signals. 500, 502, 503 and 504 are the server-side failures that typically clear
#: on their own. Other 5xx codes (501, 505, 507, 508, 510, 511) and all 4xx codes are
#: deterministic and are not retried by default.
DEFAULT_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Describe how many times to attempt delivery and how long to wait between attempts.

    The default policy performs a single attempt with no waiting, which is the engine's
    behaviour when no policy is supplied.

    The wait before retry ``n`` (1-based) is computed as::

        nominal = min(delay * backoff_factor ** (n - 1), max_delay)
        wait    = nominal * uniform(1 - jitter, 1 + jitter)
        wait    = max(wait, retry_after)   # a Retry-After header is a floor
        wait    = min(wait, max_delay)     # max_delay is always the ceiling

    Attributes:
        max_attempts: Total number of attempts, including the first one. Must be >= 1.
        delay: Seconds to wait before the second attempt; the base of the backoff curve.
        backoff_factor: Multiplier applied to the wait for each further retry. The default
            of 2.0 doubles the wait each time; use 1.0 for a constant delay.
        max_delay: Upper bound, in seconds, for any single wait. This also caps values
            taken from a ``Retry-After`` header so a misbehaving endpoint cannot block the
            caller indefinitely.
        jitter: Fraction in ``[0, 1]`` by which each wait is randomly scaled up or down,
            so that many senders failing at once do not retry in lockstep. ``0.0`` (the
            default) disables jitter; ``0.2`` yields waits between 80% and 120% of nominal.
        random_source: Zero-argument callable returning a float in ``[0, 1)``. Defaults
            to :func:`random.random`; inject a deterministic one in tests.
        retryable_status_codes: HTTP statuses that trigger another attempt.
        retry_on_transport_errors: Whether retryable :class:`TransportError` instances
            (timeouts, connection failures) trigger another attempt. Errors flagged as
            non-retryable by the transport, such as TLS failures, are never retried.
        respect_retry_after: Whether to honour a ``Retry-After`` response header when it
            asks for a longer wait than the configured delay.
    """

    max_attempts: int = 1
    delay: float = 0.0
    backoff_factor: float = 2.0
    max_delay: float = 60.0
    jitter: float = 0.0
    retryable_status_codes: frozenset[int] = field(default=DEFAULT_RETRYABLE_STATUS_CODES)
    retry_on_transport_errors: bool = True
    respect_retry_after: bool = True
    random_source: Callable[[], float] = field(default=_random.random, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ConfigurationError("max_attempts must be an integer")
        if self.max_attempts < 1:
            raise ConfigurationError("max_attempts must be at least 1")
        _require_finite_number("delay", self.delay, minimum=0.0)
        _require_finite_number("backoff_factor", self.backoff_factor, minimum=1.0)
        _require_finite_number("max_delay", self.max_delay, minimum=0.0)
        if self.delay > self.max_delay:
            raise ConfigurationError("delay must not exceed max_delay")
        _require_finite_number("jitter", self.jitter, minimum=0.0)
        if self.jitter > 1.0:
            raise ConfigurationError("jitter must be a fraction between 0 and 1")
        if not callable(self.random_source):
            raise ConfigurationError("random_source must be callable")
        if not isinstance(self.retryable_status_codes, (set, frozenset)) or any(
            isinstance(code, bool) or not isinstance(code, int)
            for code in self.retryable_status_codes
        ):
            raise ConfigurationError("retryable_status_codes must be a set of integers")
        # Normalise to an immutable set so the policy stays hashable and safe to share.
        object.__setattr__(self, "retryable_status_codes", frozenset(self.retryable_status_codes))
        object.__setattr__(self, "delay", float(self.delay))
        object.__setattr__(self, "backoff_factor", float(self.backoff_factor))
        object.__setattr__(self, "max_delay", float(self.max_delay))
        object.__setattr__(self, "jitter", float(self.jitter))

    @property
    def enabled(self) -> bool:
        """Whether this policy can ever make more than one attempt."""
        return self.max_attempts > 1

    def should_retry_response(self, response: HTTPResponse) -> bool:
        """Return ``True`` if an HTTP response warrants another attempt."""
        return response.status_code in self.retryable_status_codes

    def should_retry_error(self, error: TransportError) -> bool:
        """Return ``True`` if a transport failure warrants another attempt."""
        return self.retry_on_transport_errors and error.retryable

    def nominal_delay(self, attempt: int) -> float:
        """Seconds to wait after ``attempt`` (1-based) failed, before jitter or Retry-After.

        This is the deterministic backoff curve: ``delay * backoff_factor ** (attempt - 1)``,
        capped at ``max_delay``.
        """
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be an integer >= 1")
        wait = self.delay
        if wait == 0.0 or self.backoff_factor == 1.0:
            return wait
        # Multiply step by step and stop at the cap: ``float ** n`` raises OverflowError
        # for large ``n``, and nothing above ``max_delay`` is observable anyway.
        for _ in range(attempt - 1):
            wait *= self.backoff_factor
            if wait >= self.max_delay:
                return self.max_delay
        return wait

    def delay_before_retry(self, attempt: int, response: HTTPResponse | None = None) -> float:
        """Seconds to wait after ``attempt`` (1-based) failed, before the next attempt.

        Applies ``jitter`` to :meth:`nominal_delay`, then raises the result to any usable
        ``Retry-After`` value when ``respect_retry_after`` is set, and finally caps it at
        ``max_delay``. The result is never negative.
        """
        wait = self.nominal_delay(attempt)
        if self.jitter > 0.0 and wait > 0.0:
            sample = self.random_source()
            if not isinstance(sample, (int, float)) or not 0.0 <= sample < 1.0:
                raise ValueError("random_source must return a float in [0, 1)")
            wait *= 1.0 - self.jitter + 2.0 * self.jitter * sample
        if self.respect_retry_after and response is not None:
            retry_after = parse_retry_after(response)
            if retry_after is not None:
                wait = max(wait, retry_after)
        return max(0.0, min(wait, self.max_delay))


def parse_retry_after(response: HTTPResponse, *, now: float | None = None) -> float | None:
    """Return the ``Retry-After`` value of ``response`` in seconds, or ``None``.

    Both forms from RFC 9110 are accepted: a non-negative integer number of seconds and an
    HTTP-date. Dates in the past yield ``0.0``. Missing or unparsable headers yield ``None``.
    """
    if not response.headers:
        return None
    raw = next(
        (value for key, value in response.headers.items() if key.lower() == "retry-after"),
        None,
    )
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(int(raw))
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    current = time.time() if now is None else now
    return max(0.0, when.timestamp() - current)


def _require_finite_number(name: str, value: object, *, minimum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number")
    if not math.isfinite(value) or value < minimum:
        raise ConfigurationError(f"{name} must be a finite number >= {minimum:g}")
