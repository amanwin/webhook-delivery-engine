import pytest

from webhook_delivery import WebhookEvent


def test_event_accepts_valid_values():
    event = WebhookEvent("evt-1", "invoice.paid", {"invoice_id": 42})
    assert event.event_id == "evt-1"
    assert event.event_type == "invoice.paid"
    assert event.payload == {"invoice_id": 42}


@pytest.mark.parametrize("event_id", ["", "   ", None, 123])
def test_event_rejects_invalid_id(event_id):
    with pytest.raises(ValueError):
        WebhookEvent(event_id, "invoice.paid", {})


def test_event_rejects_empty_type():
    with pytest.raises(ValueError, match="event_type"):
        WebhookEvent("evt-1", " ", {})


def test_event_requires_dict_payload():
    with pytest.raises(TypeError, match="payload"):
        WebhookEvent("evt-1", "invoice.paid", [])


def test_event_requires_string_headers():
    with pytest.raises(TypeError, match="string keys and values"):
        WebhookEvent("evt-1", "invoice.paid", {}, {"X-Test": 1})
