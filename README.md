# Webhook Delivery Engine

A small, dependency-free Python foundation for sending webhook events to HTTP endpoints. The project is designed to grow in focused milestones, with tests around each behavior.

## Current scope

- A validated `WebhookEvent` model with a stable event ID and event type.
- A synchronous delivery engine that sends one JSON `POST` per call.
- A pluggable HTTP transport, with a standard-library `urllib` implementation.
- Configurable request timeout.
- Delivery results that distinguish 2xx responses from other HTTP statuses.
- Optional retries with a configurable attempt limit, delay, backoff and `Retry-After` support.
- A fake-transport-friendly design for deterministic tests.

Persistent queues, worker pools, rate limiting, and dead-letter handling are not implemented yet.

## Requirements

Python 3.11 or newer.

## Install for development

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
python -m pip install -e ".[dev]"
```

## Quick start

```python
from webhook_delivery import WebhookDeliveryEngine, WebhookEvent

engine = WebhookDeliveryEngine(timeout=5.0)
event = WebhookEvent(
    event_id="evt_01HXYZ",
    event_type="invoice.paid",
    payload={"invoice_id": "inv_123", "amount": 2500},
)

result = engine.deliver("https://example.com/webhooks", event)
if result.delivered:
    print("Webhook accepted", result.status_code)
else:
    print("Endpoint returned", result.status_code, result.response_body)
```

The default transport uses Python's standard library. Non-2xx HTTP responses are returned in `DeliveryResult`; transport-level failures raise `TransportError`. Without a retry policy the engine makes exactly one request.

## Retries

Retries are off unless you pass a `RetryPolicy`. Without one, `deliver()` makes exactly one request, returns any HTTP response (including 4xx/5xx) in a `DeliveryResult`, and raises `TransportError` for network-level failures.

### Usage example

```python
from webhook_delivery import (
    RetryPolicy,
    TransportError,
    WebhookDeliveryEngine,
    WebhookEvent,
)

engine = WebhookDeliveryEngine(
    timeout=5.0,
    retry=RetryPolicy(
        max_attempts=5,  # 1 initial request + up to 4 retries
        delay=1.0,  # wait 1s before the first retry...
        backoff_factor=2.0,  # ...then 2s, 4s, 8s
        max_delay=30.0,  # never wait longer than this between attempts
        jitter=0.2,  # randomise each wait by +/-20%
    ),
)

event = WebhookEvent(
    event_id="evt_01HXYZ",  # stays the same on every attempt
    event_type="invoice.paid",
    payload={"invoice_id": "inv_123", "amount": 2500},
)

try:
    result = engine.deliver("https://example.com/webhooks", event)
except TransportError as error:
    # Every attempt failed at the network level (or the failure was not retryable,
    # e.g. a TLS certificate problem). `error.retryable` says which.
    print("Could not reach endpoint:", error)
else:
    if result.delivered:
        print(f"Accepted with {result.status_code} after {result.attempts} attempt(s)")
    else:
        # The endpoint answered, but not with 2xx. Either the status was not retryable
        # (4xx, 501, ...) or it kept failing until max_attempts ran out.
        print(f"Rejected with {result.status_code} after {result.attempts} attempt(s)")
```

### How the settings work

| Setting | Default | Meaning |
|---|---|---|
| `max_attempts` | `1` | Total requests per `deliver()` call, including the first. `1` disables retries. |
| `delay` | `0.0` | Seconds to wait before the first retry. Base of the backoff curve. |
| `backoff_factor` | `2.0` | Each later wait is the previous one multiplied by this. `1.0` gives a constant delay. |
| `max_delay` | `60.0` | Ceiling for any single wait, including waits requested by `Retry-After`. `delay` may not exceed it. |
| `jitter` | `0.0` | Fraction in `[0, 1]`. Each wait is scaled by a uniform random factor in `[1 - jitter, 1 + jitter]`. |
| `retryable_status_codes` | `{408, 425, 429, 500, 502, 503, 504}` | Responses that trigger another attempt. Everything else is returned immediately. |
| `retry_on_transport_errors` | `True` | Whether retryable `TransportError`s (timeouts, refused/reset connections, DNS, dropped or malformed responses) trigger another attempt. |
| `respect_retry_after` | `True` | Whether a `Retry-After` header may lengthen the wait. |
| `random_source` | `random.random` | Zero-argument callable returning a float in `[0, 1)`; inject a fixed one in tests. |

All settings are validated when the policy is created and raise `ConfigurationError` if invalid.

**Wait before retry *n*** (1-based) is computed as:

```
nominal = min(delay * backoff_factor ** (n - 1), max_delay)
wait    = nominal * uniform(1 - jitter, 1 + jitter)
wait    = max(wait, retry_after)   # Retry-After is a floor jitter cannot undercut
wait    = min(wait, max_delay)     # max_delay is always the ceiling
```

For the example above, the nominal waits are 1, 2, 4 and 8 seconds, each then jittered by ±20%. `RetryPolicy.nominal_delay(n)` returns the un-jittered schedule if you want to log or inspect it. No wait happens after the final attempt.

**What is retried**

- HTTP `408`, `425`, `429`, `500`, `502`, `503` and `504`.
- Transport failures such as timeouts, connection refused/reset, DNS errors, and connections dropped or malformed mid-response.

**What fails immediately**, regardless of attempts remaining

- Every other status: all other `4xx` (the request or endpoint is wrong), `501`/`505`/`507`/`508`/`510`/`511`, and `3xx` responses that `urllib` could not follow. These are returned in `DeliveryResult` with `attempts == 1`.
- TLS and certificate failures. `UrllibTransport` raises these as `TransportError(retryable=False)`; custom transports can use the same flag.
- Any exception from a custom transport that is not a `TransportError`.
- Invalid endpoints, non-`WebhookEvent` arguments and non-JSON payloads, which are checked before any request is made.

**When attempts run out**, the last HTTP response is returned in `DeliveryResult` (check `delivered` and `attempts`), or the last `TransportError` is raised.

**Idempotency.** Every attempt re-sends the same `X-Webhook-Event-Id`. A timed-out attempt may still have been processed by the receiver, so receivers should use that header to deduplicate.

**Testing.** Pass `sleep=` to the engine to replace `time.sleep`, and `random_source=` to the policy to make jitter deterministic:

```python
waits = []
engine = WebhookDeliveryEngine(
    retry=RetryPolicy(max_attempts=3, delay=1.0, jitter=0.5, random_source=lambda: 0.5),
    sleep=waits.append,
)
```

## Custom transport

Pass an object with a `post(url, body, headers, timeout)` method. It should return an `HTTPResponse`. This makes the engine easy to test without network access and allows alternate HTTP clients to be added later.

## Tests and lint

```bash
python -m pytest -v
ruff check .
```

## Planned milestones

1. **Initial foundation:** event model, transport abstraction, one-shot delivery and tests. *(done)*
2. **Retry policies:** retryable status codes and transport errors, exponential backoff, jitter and attempt limits. *(done)*
3. **Persistent queue:** SQLite-backed event storage and recovery after process restart.
4. **Workers and concurrency:** background workers, bounded parallelism, graceful shutdown and rate limits.
5. **Delivery safety:** idempotency support, dead-letter handling, delivery history and metrics.
6. **Operational hardening:** configuration improvements, structured logging, security considerations and load testing.

The milestone list is a roadmap, not a claim that those features are already available.

## Security notes

Only send events to endpoints you trust. Validate and control endpoint URLs in applications that accept URLs from users, and avoid placing secrets or sensitive personal data in event payloads or logs. This library does not currently sign webhook payloads or restrict private-network destinations.

## License

MIT. See [LICENSE](LICENSE).
