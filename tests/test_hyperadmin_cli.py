"""Unit tests for the hyperadmin CLI's pure logic.

The CLI's database commands are exercised against the real schema in
``tests/test_org_provisioning.py``; what is tested here is the argument surface
and the guards that must hold *before* any write happens — a wrong exit code or a
missing `--email` should never reach the database.
"""

from __future__ import annotations

import argparse

import pytest

from lucent import hyperadmin


class TestParser:
    def test_requires_a_command(self):
        with pytest.raises(SystemExit):
            hyperadmin._build_parser().parse_args([])

    def test_status_parses(self):
        args = hyperadmin._build_parser().parse_args(["status"])
        assert args.command == "status"

    def test_grant_parses(self):
        args = hyperadmin._build_parser().parse_args(["grant", "--email", "a@b.test"])
        assert args.command == "grant"
        assert args.email == "a@b.test"
        assert args.create is False

    def test_grant_create_flag(self):
        args = hyperadmin._build_parser().parse_args(
            ["grant", "--email", "a@b.test", "--create"]
        )
        assert args.create is True

    def test_revoke_defaults_to_member(self):
        args = hyperadmin._build_parser().parse_args(["revoke", "--email", "a@b.test"])
        assert args.to == "member"

    def test_mint_key_default_name(self):
        args = hyperadmin._build_parser().parse_args(["mint-key", "--email", "a@b.test"])
        assert args.name == hyperadmin.DEFAULT_KEY_NAME


class TestMainGuards:
    def test_missing_database_url_exits_config(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert hyperadmin.main(["status"]) == hyperadmin.EXIT_CONFIG

    def test_grant_without_identity_exits_config(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://unused/unused")
        assert hyperadmin.main(["--skip-migrations", "grant"]) == hyperadmin.EXIT_CONFIG

    def test_revoke_without_identity_exits_config(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://unused/unused")
        assert hyperadmin.main(["--skip-migrations", "revoke"]) == hyperadmin.EXIT_CONFIG

    def test_mint_key_without_identity_exits_config(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://unused/unused")
        assert hyperadmin.main(["--skip-migrations", "mint-key"]) == hyperadmin.EXIT_CONFIG


class TestOperatorOrgFallback:
    def test_fallback_name_is_the_internal_org(self):
        # Operators must not be members of a customer tenant: the internal
        # secret-storage org is the only correct default home for them.
        assert hyperadmin.OPERATOR_ORG_FALLBACK == "__lucent_system__"


class TestRevokeGuard:
    def test_role_choices(self):
        args = hyperadmin._build_parser().parse_args(
            ["revoke", "--email", "a@b.test", "--to", "admin"]
        )
        assert args.to == "admin"
        assert isinstance(args, argparse.Namespace)
