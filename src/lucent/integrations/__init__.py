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
from lucent.integrations.service import IntegrationService, ServiceResult
from lucent.integrations.slack_adapter import SlackAdapter
from lucent.integrations.webhooks import WebhookSignatureMiddleware

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
    "decrypt_credential",
    "encrypt_credential",
]
