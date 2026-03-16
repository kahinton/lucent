"""Quick check for valid sessions in DB."""
import asyncio
import os

from lucent.db import init_db


async def check():
    pool = await init_db(os.environ["DATABASE_URL"])
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, display_name, session_token, session_expires_at, is_active "
            "FROM users WHERE session_token IS NOT NULL"
            " AND session_expires_at > NOW() AND is_active = true"
        )
        if row:
            tok = row["session_token"]
            print(f"User: {row['display_name']}")
            print(f"Token hash in DB: {tok[:20]}... len={len(tok)}")
            print(f"Expires: {row['session_expires_at']}")
        else:
            print("No valid sessions!")
            rows = await conn.fetch(
                "SELECT display_name, session_token IS NOT NULL as has_token,"
                " session_expires_at, is_active FROM users"
            )
            for r in rows:
                print(
                    f"  {r['display_name']}: token={r['has_token']},"
                    f" expires={r['session_expires_at']}, active={r['is_active']}"
                )

    # Also check hashing
    from lucent.auth_providers import hash_session_token
    test = "test_token_123"
    h = hash_session_token(test)
    print(f"\nHash test: input='{test}' -> hash='{h[:20]}...' len={len(h)}")

asyncio.run(check())
