"""Integrations subsystem — platform adapters for Slack, Discord, etc."""

from lucent.integrations.adapters import AdapterRegistry, AdapterResponse, DiscordAdapter
from lucent.integrations.base import IntegrationAdapter, IntegrationError
from lucent.integrations.encryption import (
    CredentialEncryptor,
    EncryptionError,
    FernetEncryptor,
    decrypt_credential,
    encrypt_credential,
)
from lucent.integrations.identity import (
    IdentityResolver,
    IdentityResult,
    PairingChallengeService,
    VerifyResult,
)
from lucent.integrations.middleware import SignatureVerificationMiddleware
from lucent.integrations.models import (
    EventType,
    IntegrationCreate,
    IntegrationEvent,
    IntegrationListResponse,
    IntegrationResponse,
    IntegrationStatus,
    IntegrationType,
    IntegrationUpdate,
    PairingChallengeCreate,
    PairingChallengeResponse,
    PairingChallengeStatus,
    PairingRedeemRequest,
    UserLinkCreate,
    UserLinkListResponse,
    UserLinkResponse,
    UserLinkStatus,
    VerificationMethod,
)
from lucent.integrations.repositories import (
    IntegrationRepo,
    PairingChallengeRepo,
    UserLinkRepo,
)


def __getattr__(name: str):
    """Lazy imports to avoid circular dependency (service → auth → db → integrations)."""
    if name == "IntegrationService":
        from lucent.integrations.service import IntegrationService
        return IntegrationService
    if name == "ServiceResult":
        from lucent.integrations.service import ServiceResult
        return ServiceResult
    if name == "admin_router":
        from lucent.integrations.router import admin_router
        return admin_router
    if name == "webhook_router":
        from lucent.integrations.router import webhook_router
        return webhook_router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
from lucent.integrations.slack_adapter import SlackAdapter  # noqa: E402
from lucent.integrations.webhooks import WebhookSignatureMiddleware  # noqa: E402

__all__ = [
    "AdapterRegistry",
    "AdapterResponse",
    "CredentialEncryptor",
    "EncryptionError",
    "EventType",
    "FernetEncryptor",
    "IdentityResolver",
    "IdentityResult",
    "IntegrationAdapter",
    "IntegrationCreate",
    "IntegrationError",
    "IntegrationEvent",
    "IntegrationListResponse",
    "IntegrationRepo",
    "IntegrationResponse",
    "IntegrationStatus",
    "IntegrationType",
    "IntegrationUpdate",
    "IntegrationService",
    "PairingChallengeCreate",
    "PairingChallengeService",
    "PairingChallengeRepo",
    "SignatureVerificationMiddleware",
    "DiscordAdapter",
    "SlackAdapter",
    "WebhookSignatureMiddleware",
    "PairingChallengeResponse",
    "PairingChallengeStatus",
    "PairingRedeemRequest",
    "ServiceResult",
    "VerifyResult",
    "UserLinkCreate",
    "UserLinkListResponse",
    "UserLinkRepo",
    "UserLinkResponse",
    "UserLinkStatus",
    "VerificationMethod",
    "admin_router",
    "decrypt_credential",
    "encrypt_credential",
    "webhook_router",
]
