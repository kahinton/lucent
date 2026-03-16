"""Quick test: provision a daemon key and immediately verify it via HTTP."""
import asyncio
import secrets

import asyncpg
import bcrypt
import httpx

DB_URL = "postgresql://lucent:lucent_dev_password@localhost:5433/lucent"
API_URL = "http://localhost:8766/api/users/me"


async def test():
    conn = await asyncpg.connect(DB_URL)
    user = await conn.fetchrow(
        "SELECT id, organization_id FROM users "
        "WHERE external_id = 'daemon-service' AND is_active = true"
    )
    user_id = str(user["id"])
    org_id = str(user["organization_id"])

    # Provision a test key
    raw_key = secrets.token_urlsafe(32)
    plain_key = f"hs_{raw_key}"
    key_prefix = plain_key[:11]
    key_hash = bcrypt.hashpw(plain_key.encode(), bcrypt.gensalt()).decode()

    row = await conn.fetchrow(
        "INSERT INTO api_keys (user_id, organization_id, name,"
        " key_prefix, key_hash, scopes, expires_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, NOW() + INTERVAL '24 hours') RETURNING id",
        user_id, org_id, "test-verify-key", key_prefix, key_hash, ["read", "write"],
    )
    key_id = str(row["id"])
    print(f"Provisioned key: {key_prefix}")

    # Now verify via HTTP
    async with httpx.AsyncClient(timeout=10) as client:
        # Try old endpoint
        resp = await client.get(API_URL, headers={"Authorization": f"Bearer {plain_key}"})
        print(f"/api/users/me: {resp.status_code}")

        # Try search endpoint (exists in all modes)
        resp2 = await client.get(
            "http://localhost:8766/api/search",
            headers={"Authorization": f"Bearer {plain_key}"},
            params={"q": "test"},
        )
        print(f"/api/search: {resp2.status_code}")

    # Clean up
    await conn.execute("DELETE FROM api_keys WHERE id = $1", key_id)
    await conn.close()
    print("Done")


asyncio.run(test())
