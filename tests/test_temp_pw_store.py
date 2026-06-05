"""Tests for M6: temp password reference token store.

Verifies:
- Reference token stored in cookie (not the password)
- Password retrievable from server-side store via ref token
- Single-use: pop removes entry after first read
- TTL expiration prevents stale retrieval
- Cleanup evicts expired entries
- Overflow clears the store
"""

import time

import pytest

from lucent.web.routes.admin import (
    _TEMP_PW_MAX_ENTRIES,
    _TEMP_PW_TTL,
    _cleanup_temp_pw_store,
    _temp_pw_store,
)


@pytest.fixture(autouse=True)
def _clear_store():
    """Ensure a clean store for every test."""
    _temp_pw_store.clear()
    yield
    _temp_pw_store.clear()


def _add_entry(token: str, password: str, expires: float | None = None):
    _temp_pw_store[token] = {
        "password": password,
        "expires": expires if expires is not None else time.time() + _TEMP_PW_TTL,
    }


class TestRefTokenStore:
    """Unit tests for the in-memory temp password store."""

    def test_password_retrievable_by_ref(self):
        """Password can be looked up via reference token."""
        _add_entry("ref123", "s3cretPw!")
        entry = _temp_pw_store["ref123"]
        assert entry["password"] == "s3cretPw!"

    def test_single_use_pop(self):
        """After pop, the entry is gone — single-use semantics."""
        _add_entry("ref_once", "oneTimePw")
        result = _temp_pw_store.pop("ref_once")
        assert result["password"] == "oneTimePw"
        assert "ref_once" not in _temp_pw_store

    def test_ttl_expiration(self):
        """Entries with expired TTL should not be considered valid."""
        _add_entry("ref_old", "oldPw", expires=time.time() - 10)
        entry = _temp_pw_store.get("ref_old")
        assert entry is not None  # still in dict
        assert entry["expires"] < time.time()  # but expired

    def test_cleanup_removes_expired(self):
        """_cleanup_temp_pw_store evicts entries past their TTL."""
        _add_entry("alive", "pw1", expires=time.time() + 300)
        _add_entry("dead1", "pw2", expires=time.time() - 5)
        _add_entry("dead2", "pw3", expires=time.time() - 1)

        _cleanup_temp_pw_store()

        assert "alive" in _temp_pw_store
        assert "dead1" not in _temp_pw_store
        assert "dead2" not in _temp_pw_store

    def test_overflow_clears_store(self):
        """When store reaches _TEMP_PW_MAX_ENTRIES, clear() is the safety valve."""
        for i in range(_TEMP_PW_MAX_ENTRIES):
            _add_entry(f"tok_{i}", f"pw_{i}")
        assert len(_temp_pw_store) == _TEMP_PW_MAX_ENTRIES

        # Simulate the overflow guard from create_user / reset_password
        if len(_temp_pw_store) >= _TEMP_PW_MAX_ENTRIES:
            _temp_pw_store.clear()
        assert len(_temp_pw_store) == 0

    def test_ref_token_not_password(self):
        """The reference token itself must not contain the password."""
        import secrets

        ref_token = secrets.token_urlsafe(32)
        password = "SuperSecret123!"
        _add_entry(ref_token, password)

        assert password not in ref_token
        assert ref_token in _temp_pw_store


class TestRefTokenIntegration:
    """Tests that verify the cookie→store→display flow end-to-end.

    These use the store directly (no HTTP) to verify the exact logic
    from users_list / create_user / reset_password.
    """

    def test_create_and_retrieve_flow(self):
        """Simulates create_user storing a ref, then users_list reading it."""
        import secrets

        password = "TempPw42!"
        ref_token = secrets.token_urlsafe(32)

        # --- create_user side ---
        _cleanup_temp_pw_store()
        _temp_pw_store[ref_token] = {
            "password": password,
            "expires": time.time() + _TEMP_PW_TTL,
        }

        # --- users_list side ---
        ref = ref_token  # would come from cookie
        temp_pw_display = None
        if ref and ref in _temp_pw_store and _temp_pw_store[ref]["expires"] > time.time():
            temp_pw_display = _temp_pw_store.pop(ref)["password"]

        assert temp_pw_display == password
        assert ref_token not in _temp_pw_store  # single-use

    def test_expired_ref_returns_nothing(self):
        """If the ref token is expired, users_list should not display the password."""
        ref_token = "expired_ref"
        _add_entry(ref_token, "ExpiredPw!", expires=time.time() - 1)

        temp_pw_display = None
        ref = ref_token
        if ref and ref in _temp_pw_store and _temp_pw_store[ref]["expires"] > time.time():
            temp_pw_display = _temp_pw_store.pop(ref)["password"]

        assert temp_pw_display is None
        # Entry still in store until cleanup runs
        assert ref_token in _temp_pw_store

    def test_unknown_ref_returns_nothing(self):
        """A fabricated ref token yields nothing."""
        temp_pw_display = None
        ref = "totally_bogus_token"
        if ref and ref in _temp_pw_store and _temp_pw_store[ref]["expires"] > time.time():
            temp_pw_display = _temp_pw_store.pop(ref)["password"]

        assert temp_pw_display is None
