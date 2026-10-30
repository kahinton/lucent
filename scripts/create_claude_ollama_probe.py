"""Create the claude+ollama probe request on the kyles-macbook remote.

Deliberately NOT using the `oclauda` backend/instance: the question is whether
the plain `claude` CLI on that host can be pointed at an Ollama model. Written to
exercise the real creation path (atomic request+tasks, session lineage, remote
pinning) so the reporting chain is tested end to end rather than hand-seeded.

Run inside the server container:
    docker exec lucent-server python /tmp/create_probe.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, "/app/src")

ORIGIN_SESSION = "172bc67c-a3f8-44ac-bdd5-635f27e8d851"
REMOTE_ID = "2cd3ec5b-f292-40c9-8dd4-4815d13489c7"  # kyles-macbook, shell executor

DESCRIPTION = """\
Determine whether the `claude` CLI on this host can be launched against a local
Ollama model.

Context: Lucent already has an `oclauda` backend that runs Claude Code over
Ollama. That pathway is OUT OF SCOPE and must not be used. The question here is
narrower: on this machine, with whatever `claude` and `ollama` are installed
independently of Lucent, can `claude` be pointed at an Ollama-served model and
actually answer?

It is a fully acceptable outcome that Claude cannot run here. What matters is
that the investigation runs end to end and reports what it found.

Report raw output for every step. Never summarize away a command's output.
"""

PROBE_SCRIPT = r'''
set -u
echo "=== STEP 1: HOST IDENTITY ==="
echo "\$ hostname"; hostname
echo "\$ sw_vers -productVersion"; sw_vers -productVersion 2>/dev/null || echo "(no sw_vers)"
echo "\$ uname -m"; uname -m

echo "=== STEP 2: IS CLAUDE INSTALLED? ==="
if command -v claude >/dev/null 2>&1; then
  echo "FOUND: $(command -v claude)"
  echo "\$ claude --version"
  claude --version 2>&1 | head -5
else
  echo "NOT FOUND: no 'claude' on PATH"
fi
echo "--- candidate install locations ---"
ls -1 "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" \
      /opt/homebrew/bin/claude /usr/local/bin/claude 2>/dev/null || true

echo "=== STEP 3: IS OLLAMA RUNNING AND WHAT MODELS? ==="
if command -v ollama >/dev/null 2>&1; then
  echo "FOUND: $(command -v ollama)"
  echo "\$ ollama list"
  ollama list 2>&1 | head -20
  echo "\$ ollama ps"
  ollama ps 2>&1 | head -10
else
  echo "NOT FOUND: no 'ollama' on PATH"
fi
echo "--- is the API listening? ---"
curl -s -m 3 http://127.0.0.1:11434/api/tags 2>&1 | head -c 600 || echo "no response from :11434"

echo "=== STEP 4: WHAT MODEL NAMES DOES OLLAMA SERVE? ==="
curl -s -m 3 http://127.0.0.1:11434/api/tags 2>/dev/null \
  | sed -n 's/.*"name":"\([^"]*\)".*/\1/p' | head -20 || true

echo "=== STEP 5: CLAUDE ENV SURFACE (does it accept a custom base URL?) ==="
echo "\$ claude --help"
claude --help 2>&1 | head -40 || echo "(claude --help unavailable)"

echo "=== STEP 6: CONCLUSION INPUTS ==="
echo "claude_present=$(command -v claude >/dev/null 2>&1 && echo yes || echo no)"
echo "ollama_present=$(command -v ollama >/dev/null 2>&1 && echo yes || echo no)"
echo "ollama_api_reachable=$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:11434/api/tags 2>/dev/null || echo 000)"
echo "=== END ==="
'''


async def main() -> None:
    from lucent.db.pool import init_db
    from lucent.db.requests import RequestRepository

    pool = await init_db(os.environ.get("DATABASE_URL"), run_migrations=False)
    org_id = os.environ.get("PROBE_ORG_ID")
    user_id = os.environ.get("PROBE_USER_ID")
    if not org_id or not user_id:
        # Derive from the session so lineage is real.
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT organization_id::text AS org_id, user_id::text AS user_id
                   FROM llm_sessions WHERE id = $1::uuid""",
                ORIGIN_SESSION,
            )
        org_id = row["org_id"]
        user_id = row["user_id"]

    repo = RequestRepository(pool)
    request = await repo.create_request_with_tasks(
        org_id=org_id,
        title="Can the Mac remote run claude against a local Ollama model?",
        description=DESCRIPTION,
        source="user",
        priority="high",
        created_by=user_id,
        task_specs=[
            {
                "title": "Probe claude + ollama on kyles-macbook",
                "description": (
                    "Run on the kyles-macbook remote. Report raw output of every "
                    "step with the command echoed before its output.\n\n"
                    "Do not use the oclauda backend or its container. This is about "
                    "the host's own claude/ollama installs.\n\n"
                    "```bash\n" + PROBE_SCRIPT.strip() + "\n```\n"
                ),
                "agent_type": "code",
                "sequence_order": 0,
                "remote_target_id": REMOTE_ID,
            }
        ],
    )
    request_id = str(request["id"])

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE requests SET origin_session_id = $2::uuid
               WHERE id = $1::uuid""",
            request_id,
            ORIGIN_SESSION,
        )
        await conn.execute(
            """INSERT INTO llm_session_requests (session_id, request_id, org_id, relation)
               VALUES ($1::uuid, $2::uuid, $3::uuid, 'created')
               ON CONFLICT DO NOTHING""",
            ORIGIN_SESSION,
            request_id,
            org_id,
        )

    print(json.dumps({"request_id": request_id, "status": request.get("status")}, indent=2))


asyncio.run(main())
