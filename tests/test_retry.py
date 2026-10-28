import ssl
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from urllib.error import URLError

import pytest

from webhook_delivery import (
    DEFAULT_RETRYABLE_STATUS_CODES,
    ConfigurationError,
    HTTPResponse,
    RetryPolicy,
    TransportError,
    UrllibTransport,
    parse_retry_after,
)


def test_default_policy_is_a_single_attempt():
    policy = RetryPolicy()
    assert policy.max_attempts == 1
    assert policy.delay == 0.0
    assert policy.enabled is False


def test_default_retryable_status_codes():
    assert DEFAULT_RETRYABLE_STATUS_CODES == frozenset({408, 425, 429, 500, 502, 503, 504})


@pytest.mark.parametrize("status", sorted(DEFAULT_RETRYABLE_STATUS_CODES))
def test_retryable_statuses(status):
    assert RetryPolicy().should_retry_response(HTTPResponse(status)) is True


@pytest.mark.parametrize("status", [200, 201, 204, 301, 400, 401, 404, 410, 422, 501, 505, 507])
def test_non_retryable_statuses(status):
    assert RetryPolicy().should_retry_response(HTTPResponse(status)) is False


def test_retryable_status_codes_can_be_customised():
    policy = RetryPolicy(retryable_status_codes={418})
    assert policy.should_retry_response(HTTPResponse(418)) is True
    assert policy.should_retry_response(HTTPResponse(503)) is False
    assert isinstance(policy.retryable_status_codes, frozenset)


def test_transport_error_retry_classification():
    policy = RetryPolicy()
    assert policy.should_retry_error(TransportError("timeout")) is True
    assert policy.should_retry_error(TransportError("tls", retryable=False)) is False
    disabled = RetryPolicy(retry_on_transport_errors=False)
    assert disabled.should_retry_error(TransportError("timeout")) is False


# --- Delay calculations ------------------------------------------------------------------


def test_backoff_doubles_by_default():
    policy = RetryPolicy(max_attempts=5, delay=2.0)
    assert policy.backoff_factor == 2.0
    assert [policy.delay_before_retry(n) for n in (1, 2, 3, 4)] == [2.0, 4.0, 8.0, 16.0]


def test_constant_delay_with_backoff_factor_one():
    policy = RetryPolicy(max_attempts=4, delay=2.0, backoff_factor=1.0)
    assert [policy.delay_before_retry(n) for n in (1, 2, 3)] == [2.0, 2.0, 2.0]


def test_exponential_backoff_with_custom_factor_and_cap():
    policy = RetryPolicy(max_attempts=6, delay=1.0, backoff_factor=3.0, max_delay=10.0)
    assert [policy.delay_before_retry(n) for n in (1, 2, 3, 4, 5)] == [1.0, 3.0, 9.0, 10.0, 10.0]


def test_fractional_backoff_factor():
    policy = RetryPolicy(max_attempts=4, delay=1.0, backoff_factor=1.5)
    assert [policy.nominal_delay(n) for n in (1, 2, 3)] == pytest.approx([1.0, 1.5, 2.25])


def test_zero_delay_stays_zero_regardless_of_backoff_and_jitter():
    policy = RetryPolicy(max_attempts=5, delay=0.0, jitter=1.0, random_source=lambda: 0.999)
    assert [policy.delay_before_retry(n) for n in (1, 2, 3)] == [0.0, 0.0, 0.0]


def test_nominal_delay_is_capped_at_max_delay():
    policy = RetryPolicy(max_attempts=10, delay=1.0, max_delay=5.0)
    assert [policy.nominal_delay(n) for n in range(1, 8)] == [1.0, 2.0, 4.0, 5.0, 5.0, 5.0, 5.0]


def test_huge_attempt_numbers_do_not_overflow():
    policy = RetryPolicy(max_attempts=10_000, delay=1.0, backoff_factor=10.0, max_delay=30.0)
    assert policy.nominal_delay(10_000) == 30.0
    assert policy.delay_before_retry(10_000) == 30.0


@pytest.mark.parametrize("attempt", [0, -1, 1.0, True, "1"])
def test_delay_calculations_reject_invalid_attempt(attempt):
    with pytest.raises(ValueError):
        RetryPolicy().delay_before_retry(attempt)
    with pytest.raises(ValueError):
        RetryPolicy().nominal_delay(attempt)


# --- Jitter ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        (0.0, 8.0),  # lower edge: nominal * (1 - jitter)
        (0.25, 9.0),
        (0.5, 10.0),  # midpoint: exactly nominal
        (0.75, 11.0),
        (0.999, 11.996),  # approaches nominal * (1 + jitter)
    ],
)
def test_jitter_scales_nominal_delay_symmetrically(sample, expected):
    policy = RetryPolicy(
        max_attempts=2, delay=10.0, backoff_factor=1.0, jitter=0.2, random_source=lambda: sample
    )
    assert policy.delay_before_retry(1) == pytest.approx(expected)
    assert policy.nominal_delay(1) == 10.0  # nominal is unaffected by jitter


def test_jitter_applies_on_top_of_exponential_growth():
    policy = RetryPolicy(max_attempts=4, delay=1.0, jitter=0.5, random_source=lambda: 0.0)
    assert [policy.delay_before_retry(n) for n in (1, 2, 3)] == pytest.approx([0.5, 1.0, 2.0])


def test_full_jitter_can_reach_zero_but_never_below():
    policy = RetryPolicy(max_attempts=2, delay=10.0, jitter=1.0, random_source=lambda: 0.0)
    assert policy.delay_before_retry(1) == 0.0


def test_jitter_is_capped_at_max_delay():
    policy = RetryPolicy(
        max_attempts=2, delay=10.0, max_delay=10.0, jitter=0.5, random_source=lambda: 0.999
    )
    assert policy.delay_before_retry(1) == 10.0


def test_jitter_cannot_undercut_retry_after():
    policy = RetryPolicy(
        max_attempts=2, delay=10.0, backoff_factor=1.0, jitter=1.0, random_source=lambda: 0.0
    )
    response = HTTPResponse(429, headers={"Retry-After": "6"})
    assert policy.delay_before_retry(1, response) == 6.0


def test_jitter_still_applies_when_nominal_is_already_at_cap():
    # Requests from many senders should still be spread out once they all hit max_delay.
    policy = RetryPolicy(
        max_attempts=10, delay=1.0, max_delay=8.0, jitter=0.25, random_source=lambda: 0.0
    )
    assert policy.nominal_delay(9) == 8.0
    assert policy.delay_before_retry(9) == pytest.approx(6.0)


def test_zero_jitter_does_not_consume_randomness():
    def explode():
        raise AssertionError("random_source must not be called when jitter is 0")

    policy = RetryPolicy(max_attempts=3, delay=1.0, random_source=explode)
    assert policy.delay_before_retry(1) == 1.0


def test_jitter_with_real_random_stays_within_bounds():
    policy = RetryPolicy(max_attempts=2, delay=10.0, backoff_factor=1.0, jitter=0.3)
    waits = [policy.delay_before_retry(1) for _ in range(2000)]
    assert all(7.0 <= w < 13.0 for w in waits)
    assert min(waits) < 8.0 and max(waits) > 12.0  # actually spread, not constant
    assert 9.0 < sum(waits) / len(waits) < 11.0  # centred on the nominal delay


@pytest.mark.parametrize("bad_sample", [-0.1, 1.0, 1.5, "0.5", None])
def test_random_source_outside_unit_interval_is_rejected(bad_sample):
    policy = RetryPolicy(max_attempts=2, delay=1.0, jitter=0.5, random_source=lambda: bad_sample)
    with pytest.raises(ValueError, match=r"\[0, 1\)"):
        policy.delay_before_retry(1)


def test_policy_equality_ignores_random_source():
    assert RetryPolicy(jitter=0.1, random_source=lambda: 0.5) == RetryPolicy(
        jitter=0.1, random_source=lambda: 0.1
    )


def test_retry_after_is_used_when_larger_than_configured_delay():
    policy = RetryPolicy(max_attempts=2, delay=1.0)
    assert policy.delay_before_retry(1, HTTPResponse(429, headers={"Retry-After": "5"})) == 5.0
    assert policy.delay_before_retry(1, HTTPResponse(429, headers={"Retry-After": "0"})) == 1.0


def test_retry_after_can_be_ignored():
    policy = RetryPolicy(max_attempts=2, delay=1.0, respect_retry_after=False)
    assert policy.delay_before_retry(1, HTTPResponse(429, headers={"Retry-After": "5"})) == 1.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": -1},
        {"max_attempts": True},
        {"max_attempts": 2.0},
        {"max_attempts": "3"},
        {"delay": -0.1},
        {"delay": float("inf")},
        {"delay": float("nan")},
        {"delay": "1"},
        {"delay": 120.0},  # exceeds default max_delay of 60
        {"backoff_factor": 0.5},
        {"backoff_factor": 0},
        {"backoff_factor": float("inf")},
        {"backoff_factor": "2"},
        {"max_delay": -1},
        {"max_delay": float("nan")},
        {"jitter": -0.1},
        {"jitter": 1.01},
        {"jitter": float("nan")},
        {"jitter": "0.5"},
        {"jitter": True},
        {"random_source": 0.5},
        {"retryable_status_codes": [503]},
        {"retryable_status_codes": {"503"}},
        {"retryable_status_codes": {True}},
    ],
)
def test_invalid_policy_is_rejected(kwargs):
    with pytest.raises(ConfigurationError):
        RetryPolicy(**kwargs)


def test_large_delay_is_allowed_when_max_delay_is_raised():
    policy = RetryPolicy(max_attempts=2, delay=120.0, max_delay=300.0)
    assert policy.delay_before_retry(1) == 120.0


# --- parse_retry_after -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (None, None),
        ({}, None),
        ({"Content-Type": "text/plain"}, None),
        ({"Retry-After": "120"}, 120.0),
        ({"retry-after": " 3 "}, 3.0),
        ({"RETRY-AFTER": "0"}, 0.0),
        ({"Retry-After": ""}, None),
        ({"Retry-After": "-5"}, None),
        ({"Retry-After": "soon"}, None),
        ({"Retry-After": "1.5"}, None),
    ],
)
def test_parse_retry_after_seconds(headers, expected):
    assert parse_retry_after(HTTPResponse(503, headers=headers)) == expected


def test_parse_retry_after_http_date():
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    future = format_datetime(now + timedelta(seconds=90), usegmt=True)
    past = format_datetime(now - timedelta(seconds=90), usegmt=True)
    assert parse_retry_after(
        HTTPResponse(503, headers={"Retry-After": future}), now=now.timestamp()
    ) == pytest.approx(90.0)
    assert (
        parse_retry_after(HTTPResponse(503, headers={"Retry-After": past}), now=now.timestamp())
        == 0.0
    )


# --- UrllibTransport error classification ----------------------------------------------


@pytest.mark.parametrize(
    "raised",
    [
        URLError(ssl.SSLError(1, "certificate verify failed")),
        URLError(ssl.SSLCertVerificationError("hostname mismatch")),
        ssl.SSLError(1, "handshake failure"),
    ],
)
def test_urllib_tls_failures_are_not_retryable(monkeypatch, raised):
    def fake_urlopen(request, timeout):
        raise raised

    monkeypatch.setattr("webhook_delivery.transport.urlopen", fake_urlopen)
    with pytest.raises(TransportError) as info:
        UrllibTransport().post("https://example.com", b"{}", {}, 1.0)
    assert info.value.retryable is False


@pytest.mark.parametrize(
    "raised",
    [
        TimeoutError("timed out"),
        URLError(TimeoutError("timed out")),
        URLError(ConnectionRefusedError(111, "refused")),
        ConnectionResetError(104, "reset"),
        URLError(OSError(-2, "Name or service not known")),
    ],
)
def test_urllib_network_failures_are_retryable(monkeypatch, raised):
    def fake_urlopen(request, timeout):
        raise raised

    monkeypatch.setattr("webhook_delivery.transport.urlopen", fake_urlopen)
    with pytest.raises(TransportError) as info:
        UrllibTransport().post("https://example.com", b"{}", {}, 1.0)
    assert info.value.retryable is True
