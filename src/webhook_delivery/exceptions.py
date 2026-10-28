"""Exceptions raised by the webhook delivery package."""


class WebhookDeliveryError(Exception):
    """Base exception for webhook delivery errors."""


class ConfigurationError(WebhookDeliveryError, ValueError):
    """Raised when delivery configuration is invalid."""


class TransportError(WebhookDeliveryError):
    """Raised when an HTTP request cannot be completed at the transport layer.

    ``retryable`` tells a retry policy whether another attempt could reasonably succeed.
    Timeouts and connection failures are retryable; failures that will not change between
    attempts, such as TLS certificate errors, should be raised with ``retryable=False``.
    """

    retryable: bool = True

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable
