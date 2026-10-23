#!/usr/bin/env python
"""One-off grandfather of pre-existing agent capability attachments.

Run this ONCE against an instance that was already using agents before
migration 147. It is deliberately *not* a migration: a migration would ship the
same data change to fresh installs, where there is nothing to grandfather, and
would re-run silently on every deploy.

What it does
------------
Records `approved` consent for every (user, agent, capability) where all of the
following already hold today:

  * the capability is attached to the agent,
  * the user can currently read the capability (`auth_clearances` user / group /
    org grant), and
  * the capability is not built-in (built-ins are consent-exempt in code).

Rationale: before this change an attachment plus readability was the entire
access rule. Grandfathering preserves exactly the behavior those users have
today instead of stripping capabilities out from under running agents. Anything
attached later, or to a user who gains readability later, still needs an
explicit approval.

Usage
-----
    docker-compose exec -T lucent python /app/scripts/grandfather_capability_consents.py --dry-run
    docker-compose exec -T lucent python /app/scripts/grandfather_capability_consents.py --apply

Safe to re-run: every insert is `ON CONFLICT DO NOTHING`, and existing decisions
(including explicit declines) are never overwritten.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

# (junction table, junction fk column, target table, capability_type, consent column)
SPECS = (
    ("agent_skills", "skill_id", "skill_definitions", "skill", "skill_id"),
    (
        "agent_managed_tools",
        "tool_id",
        "managed_tool_definitions",
        "managed_tool",
        "managed_tool_id",
    ),
    ("agent_hooks", "hook_id", "hook_definitions", "hook", "hook_id"),
    (
        "agent_mcp_servers",
        "mcp_server_id",
        "mcp_server_configs",
        "mcp_server",
        "mcp_server_id",
    ),
)

CANDIDATE_SQL = """
SELECT DISTINCT
    a.organization_id,
    a.id   AS agent_id,
    a.name AS agent_name,
    c.id   AS capability_id,
    c.name AS capability_name,
    u.id   AS user_id
FROM {junction} j
JOIN agent_definitions a ON a.id = j.agent_id
JOIN {target} c          ON c.id = j.{fk}
JOIN users u             ON u.organization_id = a.organization_id
JOIN auth_clearances ac  ON ac.auth_id = c.auth_id
                        AND ac.role IN ('read', 'write', 'owner')
WHERE c.organization_id = a.organization_id
  AND COALESCE(c.scope, 'instance') <> 'built-in'
  AND (
        (ac.principal_type = 'user'  AND ac.principal_id = u.id)
     OR (ac.principal_type = 'org'   AND ac.principal_id = u.organization_id)
     OR (ac.principal_type = 'group' AND ac.principal_id IN
            (SELECT group_id FROM user_groups WHERE user_id = u.id))
  )
  AND NOT EXISTS (
        SELECT 1 FROM agent_capability_consents consent
        WHERE consent.agent_id = j.agent_id
          AND consent.user_id = u.id
          AND consent.capability_type = '{ctype}'
          AND consent.{consent_col} = c.id
  )
"""

INSERT_SQL = """
INSERT INTO agent_capability_consents
    (organization_id, agent_id, capability_type, {consent_col}, user_id, state)
VALUES ($1, $2, $3, $4, $5, 'approved')
ON CONFLICT (agent_id, user_id, {consent_col}) WHERE capability_type = '{ctype}'
DO NOTHING
"""


async def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set in the environment")
    return url.replace("postgresql+asyncpg", "postgresql")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run", action="store_true", help="Report what would be inserted"
    )
    group.add_argument("--apply", action="store_true", help="Perform the inserts")
    args = parser.parse_args()

    conn = await asyncpg.connect(await _database_url())
    try:
        table_exists = await conn.fetchval(
            "SELECT to_regclass('public.agent_capability_consents') IS NOT NULL"
        )
        if not table_exists:
            sys.exit("agent_capability_consents does not exist; apply migration 147 first")

        grand_total = 0
        for junction, fk, target, ctype, consent_col in SPECS:
            rows = await conn.fetch(
                CANDIDATE_SQL.format(
                    junction=junction,
                    fk=fk,
                    target=target,
                    ctype=ctype,
                    consent_col=consent_col,
                )
            )
            grand_total += len(rows)
            print(f"{ctype}: {len(rows)} consent row(s) to grandfather")
            for row in rows[:5]:
                print(
                    f"    {row['agent_name']} <- {row['capability_name']} "
                    f"(user {row['user_id']})"
                )
            if len(rows) > 5:
                print(f"    ... and {len(rows) - 5} more")

            if args.apply and rows:
                async with conn.transaction():
                    for row in rows:
                        await conn.execute(
                            INSERT_SQL.format(ctype=ctype, consent_col=consent_col),
                            row["organization_id"],
                            row["agent_id"],
                            ctype,
                            row["capability_id"],
                            row["user_id"],
                        )

        if args.dry_run:
            print(f"\nDRY RUN: {grand_total} row(s) would be inserted. Nothing changed.")
        else:
            final = await conn.fetchval("SELECT COUNT(*) FROM agent_capability_consents")
            print(f"\nAPPLIED. agent_capability_consents now holds {final} row(s).")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
