"""Tests for cookie security in web routes.

Verifies that all cookie deletion uses proper security attributes
and that CSRF logging doesn't leak token values.
"""

from starlette.responses import Response

from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
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
                if "Max-Age=0" in value_str or 'max-age=0' in value_str:
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
        set_headers = [
            h.decode() for _, h in set_response.raw_headers if _ == b"set-cookie"
        ]
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
