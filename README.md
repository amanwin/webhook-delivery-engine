# Webhook Delivery Engine

A small, dependency-free Python foundation for sending webhook events to HTTP endpoints. The project is designed to grow in focused milestones, with tests around each behavior.

## Current scope

- A validated `WebhookEvent` model with a stable event ID and event type.
- A synchronous delivery engine that sends one JSON `POST` per call.
- A pluggable HTTP transport, with a standard-library `urllib` implementation.
- Configurable request timeout.
- Delivery results that distinguish 2xx responses from other HTTP statuses.
- A fake-transport-friendly design for deterministic tests.

Retries, persistent queues, worker pools, rate limiting, and dead-letter handling are not implemented yet.

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

The default transport uses Python's standard library. Non-2xx HTTP responses are returned in `DeliveryResult`; transport-level failures raise `TransportError`. This initial version makes one request and does not retry failures.

## Custom transport

Pass an object with a `post(url, body, headers, timeout)` method. It should return an `HTTPResponse`. This makes the engine easy to test without network access and allows alternate HTTP clients to be added later.

## Tests and lint

```bash
python -m pytest -v
ruff check .
```

## Planned milestones

1. **Initial foundation:** event model, transport abstraction, one-shot delivery and tests.
2. **Retry policies:** retryable status codes and transport errors, exponential backoff, jitter and attempt limits.
3. **Persistent queue:** SQLite-backed event storage and recovery after process restart.
4. **Workers and concurrency:** background workers, bounded parallelism, graceful shutdown and rate limits.
5. **Delivery safety:** idempotency support, dead-letter handling, delivery history and metrics.
6. **Operational hardening:** configuration improvements, structured logging, security considerations and load testing.

The milestone list is a roadmap, not a claim that those features are already available.

## Security notes

Only send events to endpoints you trust. Validate and control endpoint URLs in applications that accept URLs from users, and avoid placing secrets or sensitive personal data in event payloads or logs. This library does not currently sign webhook payloads or restrict private-network destinations.

## License

MIT. See [LICENSE](LICENSE).
