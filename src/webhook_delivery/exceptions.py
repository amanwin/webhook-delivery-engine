"""Exceptions raised by the webhook delivery package."""


class WebhookDeliveryError(Exception):
    """Base exception for webhook delivery errors."""


class ConfigurationError(WebhookDeliveryError, ValueError):
    """Raised when delivery configuration is invalid."""


class TransportError(WebhookDeliveryError):
    """Raised when an HTTP request cannot be completed at the transport layer."""
