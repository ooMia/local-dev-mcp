"""GitHub webhook authentication boundary; no receiver or worker is started on import."""

from .verify import (
    MAX_BODY_BYTES,
    InvalidWebhook,
    VerifiedDelivery,
    VerifiedTarget,
    supports_target_action,
    verify_delivery,
    verify_signature,
)

__all__ = [
    "MAX_BODY_BYTES",
    "InvalidWebhook",
    "VerifiedDelivery",
    "VerifiedTarget",
    "supports_target_action",
    "verify_delivery",
    "verify_signature",
]
