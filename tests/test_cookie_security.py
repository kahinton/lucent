"""Tests for cookie security in web routes.

Verifies that all cookie deletion uses proper security attributes,
that CSRF logging doesn't leak token values, and that session
regeneration occurs during impersonation to prevent session fixation.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from starlette.responses import Response

from lucent.auth_providers import (
    SESSION_COOKIE_NAME,
    get_cookie_params,
)


class TestCookieDeletionSecurity:
    """Verify delete_cookie calls include proper security params."""

    def _get_delete_cookie_headers(self, response: Response) -> list[dict]:
        """Extract Set-Cookie deletion headers from a response."""
        deletions = []
        for header_name, header_value in response.raw_headers:
            if header_name == b"set-cookie":
                value_str = header_value.decode()
                if "Max-Age=0" in value_str or "max-age=0" in value_str:
                    deletions.append(value_str)
        return deletions

    def test_delete_cookie_includes_httponly(self):
        """Deleted cookies must include HttpOnly flag."""
        response = Response()
        params = get_cookie_params()
        response.delete_cookie(key=SESSION_COOKIE_NAME, **params)

        deletions = self._get_delete_cookie_headers(response)
        assert len(deletions) == 1
        assert "httponly" in deletions[0].lower()

    def test_delete_cookie_includes_samesite(self):
        """Deleted cookies must include SameSite flag."""
        response = Response()
        params = get_cookie_params()
        response.delete_cookie(key=SESSION_COOKIE_NAME, **params)

        deletions = self._get_delete_cookie_headers(response)
        assert len(deletions) == 1
        assert "samesite=lax" in deletions[0].lower()

    def test_delete_cookie_includes_path(self):
        """Deleted cookies must include Path=/."""
        response = Response()
        params = get_cookie_params()
        response.delete_cookie(key=SESSION_COOKIE_NAME, **params)

        deletions = self._get_delete_cookie_headers(response)
        assert len(deletions) == 1
        assert "path=/" in deletions[0].lower()

    def test_delete_cookie_params_match_set_cookie_params(self):
        """Cookie deletion params should match the params used for setting."""
        params = get_cookie_params()

        set_response = Response()
        set_response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value="test_token",
            max_age=3600,
            **params,
        )

        del_response = Response()
        del_response.delete_cookie(key=SESSION_COOKIE_NAME, **params)

        # Extract and compare security attributes
        set_headers = [h.decode() for _, h in set_response.raw_headers if _ == b"set-cookie"]
        del_headers = self._get_delete_cookie_headers(del_response)

        assert len(set_headers) == 1
        assert len(del_headers) == 1

        # Both should have httponly
        assert "httponly" in set_headers[0].lower()
        assert "httponly" in del_headers[0].lower()

        # Both should have samesite
        assert "samesite=lax" in set_headers[0].lower()
        assert "samesite=lax" in del_headers[0].lower()

        # Both should have path
        assert "path=/" in set_headers[0].lower()
        assert "path=/" in del_headers[0].lower()


class TestCSRFLogSanitization:
    """Verify CSRF logging doesn't leak token values."""

    def test_check_csrf_debug_log_no_token_values(self):
        """CSRF debug logging must not include actual token values."""
        import inspect

        from lucent.web.routes import _check_csrf

        source = inspect.getsource(_check_csrf)

        # The source should not contain token slicing patterns that leak values
        assert "cookie_token[:20]" not in source
        assert "form_token[:20]" not in source

    def test_check_csrf_source_uses_safe_logging(self):
        """CSRF function should use presence-based logging, not value-based."""
        import inspect

        from lucent.web.routes import _check_csrf

        source = inspect.getsource(_check_csrf)

        # Should use presence indicators, not values
        assert '"present"' in source or "'present'" in source
        assert '"NONE"' in source or "'NONE'" in source


class TestImpersonationSessionRegeneration:
    """Verify session is regenerated when starting impersonation.

    Session fixation prevention: when an admin starts impersonating another
    user, the old session must be invalidated and a new session token issued.
    """

    def test_start_impersonation_calls_create_session(self):
        """The start_impersonation endpoint must call create_session to regenerate."""
        from lucent.web.routes import start_impersonation

        source = inspect.getsource(start_impersonation)

        # Must call create_session before setting cookies
        assert "create_session" in source, (
            "start_impersonation must call create_session to regenerate the session"
        )

    def test_start_impersonation_sets_new_session_cookie(self):
        """The response must include a new session cookie after regeneration."""
        from lucent.web.routes import start_impersonation

        source = inspect.getsource(start_impersonation)

        assert "SESSION_COOKIE_NAME" in source, "start_impersonation must set a new session cookie"

    def test_start_impersonation_binds_impersonation_to_session(self):
        """The impersonation cookie must be bound to the new session hash."""
        from lucent.web.routes import start_impersonation

        source = inspect.getsource(start_impersonation)

        # Must hash the new token and embed it in the impersonation cookie
        assert "hash_session_token" in source, (
            "start_impersonation must bind impersonation cookie to session hash"
        )
        assert "session_hash" in source or "new_token" in source, (
            "start_impersonation must use the new session token for binding"
        )

    def test_session_regeneration_before_cookie_setting(self):
        """Session regeneration must happen before setting cookies."""
        from lucent.web.routes import start_impersonation

        source = inspect.getsource(start_impersonation)

        # create_session must appear before set_cookie
        create_pos = source.index("create_session")
        set_cookie_pos = source.index("set_cookie")
        assert create_pos < set_cookie_pos, "create_session must be called before setting cookies"

    @pytest.mark.asyncio
    async def test_start_impersonation_regenerates_session_token(self):
        """Integration: verify old session token is replaced on impersonation."""
        from lucent.web.routes import start_impersonation

        admin_id = uuid4()
        target_id = uuid4()
        org_id = uuid4()

        mock_user = MagicMock()
        mock_user.id = admin_id
        mock_user.role = "owner"
        mock_user.organization_id = org_id
        mock_user.display_name = "Admin"

        mock_target = {
            "id": target_id,
            "role": "member",
            "organization_id": org_id,
            "email": "target@test.com",
            "display_name": "Target",
        }

        mock_pool = AsyncMock()
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = mock_target

        new_token = "new_session_token_abc123"

        with (
            patch("lucent.web.routes.admin.is_team_mode", return_value=True),
            patch("lucent.web.routes.admin._check_csrf", new_callable=AsyncMock),
            patch(
                "lucent.web.routes.admin.get_user_context",
                new_callable=AsyncMock,
                return_value=mock_user,
            ),
            patch(
                "lucent.web.routes.admin.get_pool",
                new_callable=AsyncMock,
                return_value=mock_pool,
            ),
            patch(
                "lucent.web.routes.admin.UserRepository",
                return_value=mock_repo,
            ),
            patch(
                "lucent.web.routes.admin.create_session",
                new_callable=AsyncMock,
                return_value=new_token,
            ) as mock_create,
        ):
            mock_request = MagicMock()
            response = await start_impersonation(mock_request, target_id)

            # Verify create_session was called with the admin's user ID
            mock_create.assert_called_once_with(mock_pool, admin_id)

            # Verify the response sets a new session cookie with the new token
            set_cookie_headers = [h.decode() for _, h in response.raw_headers if _ == b"set-cookie"]
            session_cookies = [h for h in set_cookie_headers if SESSION_COOKIE_NAME in h]
            assert len(session_cookies) >= 1, "Response must set a new session cookie"
            assert new_token in session_cookies[0], (
                "Session cookie must contain the new regenerated token"
            )
