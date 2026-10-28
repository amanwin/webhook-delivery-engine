"""Core event and delivery result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """An event to send to a webhook endpoint.

    ``event_id`` should be stable across redelivery attempts so receivers can use it
    for deduplication when later milestones add retries and persistence.
    """

    event_id: str
    event_type: str
    payload: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("event_id must be a non-empty string")
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise ValueError("event_type must be a non-empty string")
        if not isinstance(self.payload, dict):
            raise TypeError("payload must be a dictionary")
        if not isinstance(self.headers, dict):
            raise TypeError("headers must be a dictionary")
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.headers.items()
        ):
            raise TypeError("event headers must contain string keys and values")
