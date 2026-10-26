"""Session handling for the ops console.

The console is a local tool, so its session model is intentionally simple and
self-contained: a signed cookie holding an expiry timestamp, verified with an
HMAC over the console's signing secret. No server-side store, no database.

What it protects: the console *UI*. Reaching the console still requires the
hyperadmin API key, which stays on the server side of this process and is never
placed in a cookie or a template.

Threat model this is adequate for: the console binds to loopback, is used by one
person at a time, and holds no data of its own. The cookie is HttpOnly and
SameSite=Lax; CSRF additionally requires a matching token on every POST, so a
malicious page in another tab cannot drive a tenant deletion.
"""

from __future__ import annotations

import hmac
import secrets
import time
from hashlib import sha256

from fastapi import HTTPException, Request

SESSION_COOKIE = "lucent_ops_session"
CSRF_COOKIE = "lucent_ops_csrf"
CSRF_FIELD = "csrf_token"


def _sign(secret: str, value: str) -> str:
    return hmac.new(secret.encode(), value.encode(), sha256).hexdigest()


def issue_session(secret: str, *, hours: int) -> str:
    """Create an opaque session value ``<expiry>.<signature>``."""
    expires = int(time.time()) + hours * 3600
    payload = str(expires)
    return f"{payload}.{_sign(secret, payload)}"


def session_is_valid(secret: str, value: str | None) -> bool:
    """Verify a session cookie's signature and expiry."""
    if not value or "." not in value:
        return False
    payload, signature = value.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(secret, payload)):
        return False
    try:
        expires = int(payload)
    except ValueError:
        return False
    return expires > int(time.time())


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def cookie_params(*, secure: bool) -> dict:
    """Shared cookie attributes. Path is the whole console."""
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
        "path": "/",
    }


class ConsoleAuth:
    """Dependency bundle: config + request → authenticated operator."""

    def __init__(self, config):
        self.config = config

    def is_authenticated(self, request: Request) -> bool:
        return session_is_valid(
            self.config.signing_secret, request.cookies.get(SESSION_COOKIE)
        )

    def require(self, request: Request) -> None:
        """Raise a redirect-to-login if the operator is not signed in."""
        if not self.is_authenticated(request):
            raise HTTPException(
                status_code=303,
                headers={"Location": "/login"},
                detail="Sign in to the ops console.",
            )

    def require_csrf(self, request: Request, submitted: str | None) -> None:
        """Enforce the double-submit CSRF check on state-changing requests."""
        expected = request.cookies.get(CSRF_COOKIE)
        if not expected or not submitted or not hmac.compare_digest(expected, submitted):
            raise HTTPException(
                status_code=403,
                detail="CSRF validation failed. Reload the page and try again.",
            )
