import json

import pytest

from webhook_delivery import (
    ConfigurationError,
    HTTPResponse,
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


@pytest.mark.parametrize("endpoint", ["", "not-a-url", "ftp://example.com/hooks", "https:///missing-host"])
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
