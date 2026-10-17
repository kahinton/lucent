"""Lightweight authentication endpoints.

The daemon periodically probes the server to confirm that its own API key is
still valid. That probe must be *cheap* and must exercise the real auth path
(prefix lookup + hash check + expiry/revocation + organization status) so an
HTTP 401/403 remains a definitive rejection.

Historically the probe reused ``GET /api/search`` (a heavy endpoint). This
router owns the dedicated, always-registered endpoint the probe now targets:
``GET /api/auth/verify``. Unlike ``/api/users/me`` it is registered in *every*
mode (the users router is team-only), so a personal-mode daemon can use it.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from lucent.api.deps import AuthenticatedUser

router = APIRouter()


class AuthVerifyResponse(BaseModel):
    """Minimal proof-of-life response. Deliberately carries no user data."""

    valid: bool


@router.get("/verify", response_model=AuthVerifyResponse)
async def verify_auth(user: AuthenticatedUser) -> AuthVerifyResponse:
    """Confirm that the presented API key is valid.

    The ``AuthenticatedUser`` dependency performs the actual verification:
    a single indexed lookup of the key by its ``key_prefix`` (see
    ``ApiKeyRepository.verify``), the bcrypt hash check, the expiry/revocation
    checks, and the organization-status check. It raises 401 for an
    invalid/expired/revoked key and 403 for a suspended organization, so a 200
    response means the key is definitively valid. No user data is returned.
    """
    return AuthVerifyResponse(valid=True)
