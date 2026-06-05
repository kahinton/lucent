from __future__ import annotations

import argparse
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from scripts.migrate_secrets_to_transit import (
    MigrationStats,
    _build_fernet,
    _verify_transit,
    migrate_secrets,
    run_migration,
)


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _make_conn(rows: list[dict]) -> AsyncMock:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_Tx())
    return conn


def _fernet_token(secret_key: str, plaintext: str) -> str:
    return _build_fernet(secret_key).encrypt(plaintext.encode("utf-8")).decode("utf-8")


def _transit_ciphertext(plaintext: str) -> str:
    return f"vault:v1:{base64.b64encode(plaintext.encode('utf-8')).decode('ascii')}"


@pytest.mark.asyncio
async def test_migrate_secrets_mixed_data_and_idempotent():
    secret_key = "migration-test-key"
    fernet = _build_fernet(secret_key)

    rows = [
        {"id": "1", "key": "already", "encrypted_value": b"vault:v1:existing"},
        {
            "id": "2",
            "key": "needs-migrate",
            "encrypted_value": _fernet_token(secret_key, "alpha").encode("utf-8"),
        },
    ]
    conn = _make_conn(rows)

    client = AsyncMock()
    enc_resp = MagicMock()
    enc_resp.status_code = 200
    enc_resp.json.return_value = {"data": {"ciphertext": _transit_ciphertext("alpha")}}
    client.post = AsyncMock(return_value=enc_resp)

    first = await migrate_secrets(
        conn,
        client,
        fernet,
        dry_run=False,
        transit_mount="transit",
        transit_key="lucent-secrets",
    )
    assert first == MigrationStats(migrated=1, skipped=1, errors=0)
    assert conn.execute.call_count == 1

    rows_second = [
        {"id": "1", "key": "already", "encrypted_value": b"vault:v1:existing"},
        {"id": "2", "key": "needs-migrate", "encrypted_value": b"vault:v1:new"},
    ]
    conn_second = _make_conn(rows_second)
    second = await migrate_secrets(
        conn_second,
        client,
        fernet,
        dry_run=False,
        transit_mount="transit",
        transit_key="lucent-secrets",
    )
    assert second == MigrationStats(migrated=0, skipped=2, errors=0)
    conn_second.execute.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_does_not_write():
    secret_key = "migration-test-key"
    fernet = _build_fernet(secret_key)
    rows = [
        {
            "id": "2",
            "key": "needs-migrate",
            "encrypted_value": _fernet_token(secret_key, "alpha").encode("utf-8"),
        }
    ]
    conn = _make_conn(rows)
    client = AsyncMock()

    stats = await migrate_secrets(
        conn,
        client,
        fernet,
        dry_run=True,
        transit_mount="transit",
        transit_key="lucent-secrets",
    )

    assert stats == MigrationStats(migrated=1, skipped=0, errors=0)
    conn.execute.assert_not_called()
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_error_handling_invalid_key_and_unreachable_openbao():
    correct_key = "correct-key"
    wrong_key = "wrong-key"
    rows = [
        {
            "id": "1",
            "key": "bad-decrypt",
            "encrypted_value": _fernet_token(correct_key, "secret").encode("utf-8"),
        }
    ]
    conn = _make_conn(rows)
    client = AsyncMock()

    stats = await migrate_secrets(
        conn,
        client,
        _build_fernet(wrong_key),
        dry_run=False,
        transit_mount="transit",
        transit_key="lucent-secrets",
    )
    assert stats == MigrationStats(migrated=0, skipped=0, errors=1)

    verify_client = AsyncMock()
    verify_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(httpx.HTTPError):
        await _verify_transit(verify_client, "transit", "lucent-secrets")


@pytest.mark.asyncio
async def test_mixed_data_with_unknown_ciphertext_counts_error():
    secret_key = "migration-test-key"
    fernet = _build_fernet(secret_key)
    rows = [
        {"id": "1", "key": "already", "encrypted_value": b"vault:v1:existing"},
        {
            "id": "2",
            "key": "fernet",
            "encrypted_value": _fernet_token(secret_key, "alpha").encode("utf-8"),
        },
        {"id": "3", "key": "unknown", "encrypted_value": b"not-a-known-format"},
    ]
    conn = _make_conn(rows)

    client = AsyncMock()
    enc_resp = MagicMock()
    enc_resp.status_code = 200
    enc_resp.json.return_value = {"data": {"ciphertext": _transit_ciphertext("alpha")}}
    client.post = AsyncMock(return_value=enc_resp)

    stats = await migrate_secrets(
        conn,
        client,
        fernet,
        dry_run=False,
        transit_mount="transit",
        transit_key="lucent-secrets",
    )

    assert stats == MigrationStats(migrated=1, skipped=1, errors=1)


@pytest.mark.asyncio
async def test_run_migration_returns_nonzero_when_openbao_unreachable():
    args = argparse.Namespace(
        dry_run=False,
        database_url="postgresql://db",
        vault_addr="http://openbao:8200",
        vault_token="token",
        secret_key="secret-key",
        transit_mount="transit",
        transit_key="lucent-secrets",
    )

    with patch("scripts.migrate_secrets_to_transit.asyncpg.connect", new_callable=AsyncMock), patch(
        "scripts.migrate_secrets_to_transit.httpx.AsyncClient"
    ) as client_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False
        client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
        client_cls.return_value = client

        code = await run_migration(args)

    assert code == 1
