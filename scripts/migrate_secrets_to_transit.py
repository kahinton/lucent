#!/usr/bin/env python3
"""Migrate secrets from builtin (Fernet) to Transit (OpenBao) encryption."""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx
from cryptography.fernet import Fernet, InvalidToken

from lucent.secrets.builtin import _derive_fernet_key

logger = logging.getLogger("migrate_secrets_to_transit")

TRANSIT_PREFIX = "vault:v1:"
FERNET_PREFIX = "gAAAAA"


@dataclass
class MigrationStats:
    migrated: int = 0
    skipped: int = 0
    errors: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate secrets to Transit encryption")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without changing data",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL connection string",
    )
    parser.add_argument(
        "--vault-addr",
        default=os.environ.get("VAULT_ADDR"),
        help="OpenBao/Vault address",
    )
    parser.add_argument(
        "--vault-token",
        default=os.environ.get("VAULT_TOKEN"),
        help="OpenBao/Vault token",
    )
    parser.add_argument(
        "--secret-key",
        default=os.environ.get("LUCENT_SECRET_KEY"),
        help="Fernet encryption key for decrypting existing secrets",
    )
    parser.add_argument(
        "--transit-mount",
        default="transit",
        help="Transit engine mount path",
    )
    parser.add_argument(
        "--transit-key",
        default="lucent-secrets",
        help="Transit key name",
    )
    return parser.parse_args()


def _require_args(args: argparse.Namespace) -> None:
    missing: list[str] = []
    if not args.database_url:
        missing.append("DATABASE_URL / --database-url")
    if not args.vault_addr:
        missing.append("VAULT_ADDR / --vault-addr")
    if not args.vault_token:
        missing.append("VAULT_TOKEN / --vault-token")
    if not args.secret_key:
        missing.append("LUCENT_SECRET_KEY / --secret-key")
    if missing:
        raise ValueError(f"Missing required configuration: {', '.join(missing)}")


def _to_text(value: Any) -> str:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _is_transit_ciphertext(value: str) -> bool:
    return value.startswith(TRANSIT_PREFIX)


def _is_fernet_ciphertext(value: str) -> bool:
    return value.startswith(FERNET_PREFIX)


def _build_fernet(secret_key: str) -> Fernet:
    return Fernet(_derive_fernet_key(secret_key))


async def _verify_transit(client: httpx.AsyncClient, mount: str, key: str) -> None:
    health_resp = await client.get("/v1/sys/health")
    if health_resp.status_code != 200:
        raise RuntimeError(f"OpenBao health check failed: {health_resp.status_code}")

    key_resp = await client.get(f"/v1/{mount}/keys/{key}")
    if key_resp.status_code != 200:
        raise RuntimeError(
            f"Transit key check failed for {mount}/{key}: {key_resp.status_code}"
        )


async def _transit_encrypt(
    client: httpx.AsyncClient,
    plaintext: str,
    mount: str,
    key: str,
) -> str:
    encoded = plaintext.encode("utf-8")
    b64_plaintext = base64.b64encode(encoded).decode("ascii")
    resp = await client.post(
        f"/v1/{mount}/encrypt/{key}",
        json={"plaintext": b64_plaintext},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Transit encrypt failed: HTTP {resp.status_code}")
    ciphertext = resp.json().get("data", {}).get("ciphertext")
    if not ciphertext:
        raise RuntimeError("Transit encrypt response missing ciphertext")
    return ciphertext


async def migrate_secrets(
    conn: asyncpg.Connection,
    client: httpx.AsyncClient,
    fernet: Fernet,
    *,
    dry_run: bool,
    transit_mount: str,
    transit_key: str,
) -> MigrationStats:
    stats = MigrationStats()
    rows = await conn.fetch(
        "SELECT id, key, encrypted_value FROM secrets ORDER BY created_at, id"
    )

    for row in rows:
        secret_id = row["id"]
        secret_key = row["key"]
        try:
            encrypted_text = _to_text(row["encrypted_value"])

            if _is_transit_ciphertext(encrypted_text):
                stats.skipped += 1
                logger.info(
                    "Skipping already-migrated secret key=%s id=%s",
                    secret_key,
                    secret_id,
                )
                continue

            if not _is_fernet_ciphertext(encrypted_text):
                stats.errors += 1
                logger.error(
                    "Unsupported ciphertext format for key=%s id=%s (expected Fernet or Transit)",
                    secret_key,
                    secret_id,
                )
                continue

            plaintext = fernet.decrypt(encrypted_text.encode("utf-8")).decode("utf-8")

            if dry_run:
                stats.migrated += 1
                logger.info("Would migrate key=%s id=%s", secret_key, secret_id)
                continue

            ciphertext = await _transit_encrypt(
                client,
                plaintext,
                mount=transit_mount,
                key=transit_key,
            )
            async with conn.transaction():
                await conn.execute(
                    "UPDATE secrets SET encrypted_value = $1, updated_at = NOW() WHERE id = $2",
                    ciphertext.encode("utf-8"),
                    secret_id,
                )
            stats.migrated += 1
            logger.info("Migrated key=%s id=%s", secret_key, secret_id)
        except InvalidToken:
            stats.errors += 1
            logger.error(
                "Fernet decrypt failed for key=%s id=%s (wrong key or corrupted data)",
                secret_key,
                secret_id,
            )
        except Exception as exc:
            stats.errors += 1
            logger.error("Failed to migrate key=%s id=%s: %s", secret_key, secret_id, exc)

    return stats


async def run_migration(args: argparse.Namespace) -> int:
    _require_args(args)
    fernet = _build_fernet(args.secret_key)

    async with httpx.AsyncClient(
        base_url=args.vault_addr.rstrip("/"),
        headers={"X-Vault-Token": args.vault_token},
        timeout=10.0,
    ) as client:
        try:
            await _verify_transit(client, args.transit_mount, args.transit_key)
        except httpx.HTTPError as exc:
            logger.error("OpenBao connectivity check failed: %s", exc)
            return 1
        except Exception as exc:
            logger.error("Transit verification failed: %s", exc)
            return 1

        conn = await asyncpg.connect(args.database_url)
        try:
            stats = await migrate_secrets(
                conn,
                client,
                fernet,
                dry_run=args.dry_run,
                transit_mount=args.transit_mount,
                transit_key=args.transit_key,
            )
        finally:
            await conn.close()

    action_word = "would be migrated" if args.dry_run else "migrated"
    print(
        f"Migration complete: {stats.migrated} {action_word}, "
        f"{stats.skipped} already migrated (skipped), {stats.errors} errors"
    )
    return 1 if stats.errors > 0 else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    try:
        exit_code = asyncio.run(run_migration(args))
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(2)
    except KeyboardInterrupt:
        logger.error("Interrupted")
        sys.exit(130)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
