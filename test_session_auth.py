"""Test session token auth flow.

We can't get the raw session cookie (HttpOnly), so we'll simulate creating
one and testing validate_session with it.
"""
import asyncio
import os
import secrets
from lucent.db import init_db
from lucent.auth_providers import hash_session_token, validate_session


async def test():
    pool = await init_db(os.environ["DATABASE_URL"])

    # Create a known session token and store its hash
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_session_token(raw_token)
    print(f"Raw token: {raw_token[:20]}... len={len(raw_token)}")
    print(f"Hash: {token_hash[:20]}... len={len(token_hash)}")

    # Store it on the user
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET session_token = $1, session_expires_at = NOW() + interval '1 hour' "
            "WHERE display_name = 'kahinton'",
            token_hash,
        )
        print("Updated user session token in DB")

    # Now test validate_session with the raw token
    user = await validate_session(pool, raw_token)
    if user:
        print(f"SUCCESS: validate_session returned user: {user['display_name']}")
    else:
        print("FAILURE: validate_session returned None!")

        # Debug: check what's in the DB
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT session_token, session_expires_at FROM users WHERE display_name = 'kahinton'"
            )
            print(f"DB hash: {row['session_token'][:20]}... stored_len={len(row['session_token'])}")
            print(f"Our hash: {token_hash[:20]}... our_len={len(token_hash)}")
            print(f"Match: {row['session_token'] == token_hash}")

asyncio.run(test())
