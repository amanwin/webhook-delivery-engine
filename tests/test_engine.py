import json

import pytest

from webhook_delivery import (
    ConfigurationError,
    HTTPResponse,
    RetryPolicy,
    TransportError,
    WebhookDeliveryEngine,
    WebhookEvent,
)


class FakeTransport:
    def __init__(self, response=None):
        self.response = response or HTTPResponse(204)
        self.calls = []

    def post(self, url, body, headers, timeout):
        self.calls.append((url, body, headers, timeout))
        return self.response


def make_event(**kwargs):
    defaults = {"event_id": "evt-123", "event_type": "order.created", "payload": {"id": 7}}
    defaults.update(kwargs)
    return WebhookEvent(**defaults)


def test_deliver_posts_json_and_event_headers():
    transport = FakeTransport(HTTPResponse(202, b"accepted", {"X-Request": "abc"}))
    engine = WebhookDeliveryEngine(timeout=2.5, transport=transport)

    result = engine.deliver("https://example.com/hooks", make_event())

    assert result.delivered is True
    assert result.status_code == 202
    assert result.response_body == b"accepted"
    assert result.response_headers == {"X-Request": "abc"}
    url, body, headers, timeout = transport.calls[0]
    assert url == "https://example.com/hooks"
    assert json.loads(body) == {"id": 7}
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["X-Webhook-Event-Id"] == "evt-123"
    assert headers["X-Webhook-Event-Type"] == "order.created"
    assert timeout == 2.5


def test_event_custom_headers_can_be_added():
    transport = FakeTransport()
    engine = WebhookDeliveryEngine(transport=transport)
    engine.deliver("https://example.com/hooks", make_event(headers={"X-Tenant": "acme"}))
    assert transport.calls[0][2]["X-Tenant"] == "acme"


def test_custom_header_can_override_default_header():
    transport = FakeTransport()
    engine = WebhookDeliveryEngine(transport=transport)
    engine.deliver(
        "https://example.com/hooks",
        make_event(headers={"Content-Type": "application/custom+json"}),
    )
    assert transport.calls[0][2]["Content-Type"] == "application/custom+json"


@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_2xx_response_is_successful(status):
    result = WebhookDeliveryEngine(transport=FakeTransport(HTTPResponse(status))).deliver(
        "https://example.com/hooks", make_event()
    )
    assert result.delivered is True


@pytest.mark.parametrize("status", [199, 300, 400, 429, 500])
def test_non_2xx_response_is_not_successful(status):
    result = WebhookDeliveryEngine(transport=FakeTransport(HTTPResponse(status))).deliver(
        "https://example.com/hooks", make_event()
    )
    assert result.delivered is False
    assert result.status_code == status


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "5"])
def test_invalid_timeout_is_rejected(timeout):
    with pytest.raises(ConfigurationError):
        WebhookDeliveryEngine(timeout=timeout)


@pytest.mark.parametrize(
    "endpoint", ["", "not-a-url", "ftp://example.com/hooks", "https:///missing-host"]
)
def test_invalid_endpoint_is_rejected_before_transport(endpoint):
    transport = FakeTransport()
    engine = WebhookDeliveryEngine(transport=transport)
    with pytest.raises(ConfigurationError):
        engine.deliver(endpoint, make_event())
    assert transport.calls == []


def test_non_event_argument_is_rejected():
    engine = WebhookDeliveryEngine(transport=FakeTransport())
    with pytest.raises(TypeError, match="WebhookEvent"):
        engine.deliver("https://example.com/hooks", object())


def test_non_json_payload_fails_before_transport_call():
    transport = FakeTransport()
    engine = WebhookDeliveryEngine(transport=transport)
    event = make_event(payload={"not_json": object()})
    with pytest.raises(ValueError, match="not JSON serializable"):
        engine.deliver("https://example.com/hooks", event)
    assert transport.calls == []


# --- Retry behaviour -------------------------------------------------------------------


class SequenceTransport:
    """Return (or raise) each outcome in turn; the last one repeats if exhausted."""

    def __init__(self, *outcomes):
        assert outcomes
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, body, headers, timeout):
        self.calls.append((url, body, headers, timeout))
        index = min(len(self.calls) - 1, len(self.outcomes) - 1)
        outcome = self.outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RecordingSleep:
    def __init__(self):
        self.waits = []

    def __call__(self, seconds):
        self.waits.append(seconds)


def retrying_engine(transport, **policy):
    sleep = RecordingSleep()
    engine = WebhookDeliveryEngine(transport=transport, retry=RetryPolicy(**policy), sleep=sleep)
    return engine, sleep


def test_default_engine_makes_exactly_one_attempt_on_retryable_status():
    transport = SequenceTransport(HTTPResponse(503), HTTPResponse(200))
    sleep = RecordingSleep()
    engine = WebhookDeliveryEngine(transport=transport, sleep=sleep)

    result = engine.deliver("https://example.com/hooks", make_event())

    assert result.delivered is False
    assert result.status_code == 503
    assert result.attempts == 1
    assert len(transport.calls) == 1
    assert sleep.waits == []


def test_default_engine_does_not_retry_transport_errors():
    transport = SequenceTransport(TransportError("boom"), HTTPResponse(200))
    engine = WebhookDeliveryEngine(transport=transport, sleep=RecordingSleep())

    with pytest.raises(TransportError, match="boom"):
        engine.deliver("https://example.com/hooks", make_event())
    assert len(transport.calls) == 1


def test_retryable_status_is_retried_until_success():
    transport = SequenceTransport(HTTPResponse(503), HTTPResponse(502), HTTPResponse(204))
    engine, sleep = retrying_engine(transport, max_attempts=5, delay=1.5)

    result = engine.deliver("https://example.com/hooks", make_event())

    assert result.delivered is True
    assert result.status_code == 204
    assert result.attempts == 3
    assert len(transport.calls) == 3
    assert sleep.waits == [1.5, 3.0]  # exponential by default


def test_every_attempt_sends_identical_request():
    transport = SequenceTransport(HTTPResponse(500), HTTPResponse(200))
    engine, _ = retrying_engine(transport, max_attempts=2)
    engine.deliver("https://example.com/hooks", make_event(headers={"X-Tenant": "acme"}))

    first, second = transport.calls
    assert first == second
    assert first[2]["X-Webhook-Event-Id"] == "evt-123"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422, 501, 505, 300, 199])
def test_non_retryable_status_fails_immediately(status):
    transport = SequenceTransport(HTTPResponse(status), HTTPResponse(200))
    engine, sleep = retrying_engine(transport, max_attempts=3, delay=1.0)

    result = engine.deliver("https://example.com/hooks", make_event())

    assert result.delivered is False
    assert result.status_code == status
    assert result.attempts == 1
    assert len(transport.calls) == 1
    assert sleep.waits == []


def test_exhausted_retries_return_last_response():
    transport = SequenceTransport(HTTPResponse(503, b"still down"))
    engine, sleep = retrying_engine(transport, max_attempts=3, delay=0.5)

    result = engine.deliver("https://example.com/hooks", make_event())

    assert result.delivered is False
    assert result.status_code == 503
    assert result.response_body == b"still down"
    assert result.attempts == 3
    assert len(transport.calls) == 3
    assert sleep.waits == [0.5, 1.0]  # no sleep after the final attempt


def test_transport_error_is_retried_then_succeeds():
    transport = SequenceTransport(TransportError("timed out"), HTTPResponse(200))
    engine, sleep = retrying_engine(transport, max_attempts=3, delay=2.0)

    result = engine.deliver("https://example.com/hooks", make_event())

    assert result.delivered is True
    assert result.attempts == 2
    assert sleep.waits == [2.0]


def test_exhausted_retries_raise_last_transport_error():
    transport = SequenceTransport(TransportError("first"), TransportError("second"))
    engine, sleep = retrying_engine(transport, max_attempts=2, delay=1.0)

    with pytest.raises(TransportError, match="second"):
        engine.deliver("https://example.com/hooks", make_event())
    assert len(transport.calls) == 2
    assert sleep.waits == [1.0]


def test_non_retryable_transport_error_is_not_retried():
    transport = SequenceTransport(TransportError("bad cert", retryable=False), HTTPResponse(200))
    engine, sleep = retrying_engine(transport, max_attempts=3, delay=1.0)

    with pytest.raises(TransportError, match="bad cert"):
        engine.deliver("https://example.com/hooks", make_event())
    assert len(transport.calls) == 1
    assert sleep.waits == []


def test_transport_error_retries_can_be_disabled():
    transport = SequenceTransport(TransportError("boom"), HTTPResponse(200))
    engine, _ = retrying_engine(transport, max_attempts=3, retry_on_transport_errors=False)

    with pytest.raises(TransportError):
        engine.deliver("https://example.com/hooks", make_event())
    assert len(transport.calls) == 1


def test_mixed_failures_count_towards_the_same_attempt_budget():
    transport = SequenceTransport(HTTPResponse(429), TransportError("reset"), HTTPResponse(503))
    engine, sleep = retrying_engine(transport, max_attempts=3, delay=1.0)

    result = engine.deliver("https://example.com/hooks", make_event())

    assert result.status_code == 503
    assert result.attempts == 3
    assert len(sleep.waits) == 2


def test_exponential_backoff_is_capped_by_max_delay():
    transport = SequenceTransport(HTTPResponse(503))
    engine, sleep = retrying_engine(
        transport, max_attempts=5, delay=1.0, backoff_factor=2.0, max_delay=5.0
    )

    engine.deliver("https://example.com/hooks", make_event())

    assert sleep.waits == [1.0, 2.0, 4.0, 5.0]


def test_retry_after_header_extends_the_wait():
    transport = SequenceTransport(
        HTTPResponse(429, headers={"Retry-After": "7"}), HTTPResponse(200)
    )
    engine, sleep = retrying_engine(transport, max_attempts=2, delay=1.0)

    engine.deliver("https://example.com/hooks", make_event())

    assert sleep.waits == [7.0]


def test_retry_after_header_is_capped_by_max_delay():
    transport = SequenceTransport(
        HTTPResponse(503, headers={"retry-after": "86400"}), HTTPResponse(200)
    )
    engine, sleep = retrying_engine(transport, max_attempts=2, delay=1.0, max_delay=30.0)

    engine.deliver("https://example.com/hooks", make_event())

    assert sleep.waits == [30.0]


def test_constant_delay_when_backoff_factor_is_one():
    transport = SequenceTransport(HTTPResponse(503))
    engine, sleep = retrying_engine(transport, max_attempts=4, delay=1.5, backoff_factor=1.0)

    engine.deliver("https://example.com/hooks", make_event())

    assert sleep.waits == [1.5, 1.5, 1.5]


def test_jitter_spreads_waits_using_injected_random_source():
    samples = iter([0.0, 0.5, 0.999])
    transport = SequenceTransport(HTTPResponse(503))
    engine, sleep = retrying_engine(
        transport,
        max_attempts=4,
        delay=10.0,
        backoff_factor=1.0,
        jitter=0.5,
        random_source=lambda: next(samples),
    )

    engine.deliver("https://example.com/hooks", make_event())

    assert sleep.waits == pytest.approx([5.0, 10.0, 14.99])


def test_jitter_never_exceeds_max_delay_or_undercuts_retry_after():
    transport = SequenceTransport(
        HTTPResponse(503, headers={"Retry-After": "8"}),
        HTTPResponse(503),
        HTTPResponse(200),
    )
    engine, sleep = retrying_engine(
        transport,
        max_attempts=3,
        delay=10.0,
        backoff_factor=1.0,
        max_delay=12.0,
        jitter=1.0,
        random_source=iter([0.0, 0.999]).__next__,
    )

    engine.deliver("https://example.com/hooks", make_event())

    assert sleep.waits == [8.0, 12.0]


def test_zero_delay_never_calls_sleep():
    transport = SequenceTransport(HTTPResponse(503), HTTPResponse(200))
    engine, sleep = retrying_engine(transport, max_attempts=2)

    engine.deliver("https://example.com/hooks", make_event())

    assert sleep.waits == []


def test_preflight_failures_are_not_retried():
    transport = SequenceTransport(HTTPResponse(200))
    engine, sleep = retrying_engine(transport, max_attempts=3, delay=1.0)

    with pytest.raises(ConfigurationError):
        engine.deliver("ftp://example.com", make_event())
    with pytest.raises(ValueError, match="not JSON serializable"):
        engine.deliver("https://example.com/hooks", make_event(payload={"x": object()}))
    assert transport.calls == []
    assert sleep.waits == []


def test_retry_policy_property_defaults_to_single_attempt():
    engine = WebhookDeliveryEngine(transport=FakeTransport())
    assert engine.retry_policy == RetryPolicy()
    assert engine.retry_policy.enabled is False


@pytest.mark.parametrize("retry", ["3", 3, {"max_attempts": 3}])
def test_invalid_retry_argument_is_rejected(retry):
    with pytest.raises(ConfigurationError, match="RetryPolicy"):
        WebhookDeliveryEngine(transport=FakeTransport(), retry=retry)


def test_non_callable_sleep_is_rejected():
    with pytest.raises(ConfigurationError, match="sleep"):
        WebhookDeliveryEngine(transport=FakeTransport(), sleep=1)


# --- Failure categories: what is retried, what is not --------------------------------------
#
# Each category below models one kind of real-world failure as the transport reports it:
#   connection errors / timeouts -> TransportError(retryable=True)
#   TLS failures                 -> TransportError(retryable=False)
#   server errors                -> HTTPResponse(5xx)
#   client errors                -> HTTPResponse(4xx)

TRANSIENT_FAILURES = {
    "connection-refused": TransportError("HTTP request failed: [Errno 111] Connection refused"),
    "connection-reset": TransportError("HTTP request failed: Connection reset by peer"),
    "remote-disconnected": TransportError("HTTP request failed: Remote end closed connection"),
    "dns-failure": TransportError("HTTP request failed: Name or service not known"),
    "connect-timeout": TransportError("HTTP request failed: timed out"),
    "read-timeout": TransportError("HTTP request failed: The read operation timed out"),
    "incomplete-read": TransportError("HTTP request failed: IncompleteRead(5 bytes read)"),
    "500-internal": HTTPResponse(500, b"oops"),
    "502-bad-gateway": HTTPResponse(502),
    "503-unavailable": HTTPResponse(503, headers={"Retry-After": "0"}),
    "504-gateway-timeout": HTTPResponse(504),
    "429-rate-limited": HTTPResponse(429, headers={"Retry-After": "0"}),
    "408-request-timeout": HTTPResponse(408),
}

PERMANENT_FAILURES = {
    "tls-certificate": TransportError("certificate verify failed", retryable=False),
    "400-bad-request": HTTPResponse(400, b"malformed"),
    "401-unauthorized": HTTPResponse(401),
    "403-forbidden": HTTPResponse(403),
    "404-not-found": HTTPResponse(404),
    "405-method-not-allowed": HTTPResponse(405),
    "410-gone": HTTPResponse(410),
    "413-too-large": HTTPResponse(413),
    "422-unprocessable": HTTPResponse(422),
    "501-not-implemented": HTTPResponse(501),
    "505-version-unsupported": HTTPResponse(505),
    "507-insufficient-storage": HTTPResponse(507),
    "301-redirect-not-followed": HTTPResponse(301, headers={"Location": "https://elsewhere"}),
}


def _deliver(engine):
    return engine.deliver("https://example.com/hooks", make_event())


@pytest.mark.parametrize("failure", TRANSIENT_FAILURES.values(), ids=TRANSIENT_FAILURES.keys())
def test_transient_failure_then_success_is_delivered(failure):
    transport = SequenceTransport(failure, HTTPResponse(200, b"ok"))
    engine, sleep = retrying_engine(transport, max_attempts=3, delay=1.0)

    result = _deliver(engine)

    assert result.delivered is True
    assert result.status_code == 200
    assert result.response_body == b"ok"
    assert result.attempts == 2
    assert len(transport.calls) == 2
    assert sleep.waits == [1.0]


@pytest.mark.parametrize("failure", TRANSIENT_FAILURES.values(), ids=TRANSIENT_FAILURES.keys())
def test_transient_failure_exhausts_attempts(failure):
    transport = SequenceTransport(failure)  # fails the same way every time
    engine, sleep = retrying_engine(transport, max_attempts=4, delay=1.0, backoff_factor=1.0)

    if isinstance(failure, TransportError):
        with pytest.raises(TransportError) as info:
            _deliver(engine)
        assert info.value is failure  # the last error is what surfaces
    else:
        result = _deliver(engine)
        assert result.delivered is False
        assert result.status_code == failure.status_code
        assert result.response_body == failure.body
        assert result.attempts == 4

    assert len(transport.calls) == 4
    assert sleep.waits == [1.0, 1.0, 1.0]  # waits between attempts only, none after the last


@pytest.mark.parametrize("failure", TRANSIENT_FAILURES.values(), ids=TRANSIENT_FAILURES.keys())
def test_success_on_the_final_allowed_attempt_counts_as_delivered(failure):
    transport = SequenceTransport(failure, failure, HTTPResponse(204))
    engine, _ = retrying_engine(transport, max_attempts=3)

    result = _deliver(engine)

    assert result.delivered is True
    assert result.attempts == 3


@pytest.mark.parametrize("failure", PERMANENT_FAILURES.values(), ids=PERMANENT_FAILURES.keys())
def test_permanent_failure_is_not_retried_even_though_attempts_remain(failure):
    transport = SequenceTransport(failure, HTTPResponse(200))  # a retry *would* succeed
    engine, sleep = retrying_engine(transport, max_attempts=5, delay=1.0)

    if isinstance(failure, TransportError):
        with pytest.raises(TransportError) as info:
            _deliver(engine)
        assert info.value is failure
    else:
        result = _deliver(engine)
        assert result.delivered is False
        assert result.status_code == failure.status_code
        assert result.attempts == 1

    assert len(transport.calls) == 1
    assert sleep.waits == []


def test_unexpected_exception_from_transport_is_not_retried():
    class BuggyTransport:
        calls = 0

        def post(self, url, body, headers, timeout):
            self.calls += 1
            raise RuntimeError("bug in custom transport")

    transport = BuggyTransport()
    engine, sleep = retrying_engine(transport, max_attempts=5, delay=1.0)

    with pytest.raises(RuntimeError, match="bug in custom transport"):
        _deliver(engine)
    assert transport.calls == 1
    assert sleep.waits == []


def test_transport_error_subclass_without_super_init_defaults_to_retryable():
    class LegacyError(TransportError):
        def __init__(self):  # forgets to call super().__init__
            pass

    transport = SequenceTransport(LegacyError(), HTTPResponse(200))
    engine, _ = retrying_engine(transport, max_attempts=2)

    assert _deliver(engine).delivered is True


def test_transient_then_permanent_stops_at_the_permanent_failure():
    transport = SequenceTransport(HTTPResponse(503), TransportError("reset"), HTTPResponse(404))
    engine, sleep = retrying_engine(transport, max_attempts=10, delay=1.0, backoff_factor=1.0)

    result = _deliver(engine)

    assert result.status_code == 404
    assert result.attempts == 3
    assert len(transport.calls) == 3
    assert sleep.waits == [1.0, 1.0]


def test_transient_then_tls_failure_raises_without_further_attempts():
    transport = SequenceTransport(
        TransportError("timed out"), TransportError("bad cert", retryable=False), HTTPResponse(200)
    )
    engine, sleep = retrying_engine(transport, max_attempts=10, delay=1.0)

    with pytest.raises(TransportError, match="bad cert"):
        _deliver(engine)
    assert len(transport.calls) == 2
    assert sleep.waits == [1.0]


def test_retry_budget_is_per_delivery_not_per_engine():
    transport = SequenceTransport(HTTPResponse(503), HTTPResponse(503), HTTPResponse(200))
    engine, _ = retrying_engine(transport, max_attempts=2)

    first = _deliver(engine)  # 503, 503 -> exhausted
    second = _deliver(engine)  # fresh budget: 200

    assert first.delivered is False and first.attempts == 2
    assert second.delivered is True and second.attempts == 1
