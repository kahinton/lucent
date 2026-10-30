"""Re-run the claude+ollama probe with an ACTUAL launch attempt.

The first attempt was correctly rejected by the review gate for stopping at
discovery. This version:
  1. proves the Anthropic surface is live on the local Ollama,
  2. launches `claude` non-interactively pointed at it via ANTHROPIC_BASE_URL,
  3. captures the model's real answer or the real failure,
  4. writes a durable deliverable.

Deliberately independent of the `oclauda` backend: plain env vars, plain CLI.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, "/app/src")

ORIGIN_SESSION = "172bc67c-a3f8-44ac-bdd5-635f27e8d851"
REMOTE_ID = "2cd3ec5b-f292-40c9-8dd4-4815d13489c7"

DESCRIPTION = """\
Determine, by ACTUALLY ATTEMPTING IT, whether the `claude` CLI on this host can
be pointed at a local Ollama model and produce an answer.

The previous attempt was rejected for stopping at "is it installed / what does
--help say". Do not repeat that. This task must run the CLI against the local
model and report what came back, whether it succeeded or failed.

Mechanism to use: Ollama >= 0.14 serves the Anthropic Messages API at
`/v1/messages`. Point `claude` at it with `ANTHROPIC_BASE_URL` plus an
`ANTHROPIC_AUTH_TOKEN` (any non-empty value). Keep it non-interactive with
`-p/--print`.

Hard constraint: do NOT use Lucent's `oclauda` backend or its container. Use only
this host's own `claude` and `ollama` binaries and plain environment variables.

Write the full transcript to a file so the finding is durable.

A negative result is a complete result. If claude cannot drive the local model,
say so and show the error verbatim.
"""

PROBE_SCRIPT = r'''
set -u
OUT="$HOME/.lucent-probe-claude-ollama.txt"
exec > >(tee "$OUT") 2>&1
echo "=== STEP 1: HOST + BINARIES ==="
echo "\$ hostname"; hostname
echo "\$ sw_vers -productVersion"; sw_vers -productVersion 2>/dev/null || true
CLAUDE=$(command -v claude || true)
echo "claude=$CLAUDE"
[ -n "$CLAUDE" ] && { echo "\$ claude --version"; claude --version 2>&1 | head -2; }

echo "=== STEP 2: OLLAMA SERVES THE ANTHROPIC SURFACE? ==="
echo "\$ curl -s http://127.0.0.1:11434/api/version"
curl -s -m 5 http://127.0.0.1:11434/api/version || echo "(no /api/version)"
echo
MODEL=$(curl -s -m 5 http://127.0.0.1:11434/api/tags 2>/dev/null \
  | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    names=[m["name"] for m in d.get("models",[])]
    print(next((n for n in names if "deepseek" in n), names[0] if names else ""))
except Exception:
    print("")' 2>/dev/null)
echo "MODEL selected = ${MODEL:-<none>}"

echo "\$ curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:11434/v1/messages"
ANTHROPIC_STATUS=$(curl -s -m 15 -o /tmp/anthropic_probe.json -w '%{http_code}' \
  -X POST http://127.0.0.1:11434/v1/messages \
  -H 'content-type: application/json' \
  -H 'x-api-key: probe' \
  -H 'anthropic-version: 2023-06-01' \
  -d "{\"model\":\"${MODEL}\",\"max_tokens\":32,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: SURFACE_OK\"}]}")
echo "anthropic_surface_http=$ANTHROPIC_STATUS"
echo "--- raw /v1/messages body (first 500 chars) ---"
head -c 500 /tmp/anthropic_probe.json 2>/dev/null; echo

echo "=== STEP 3: ACTUALLY LAUNCH CLAUDE AGAINST THE LOCAL MODEL ==="
if [ -z "$CLAUDE" ]; then
  echo "CANNOT LAUNCH: no claude binary"
elif [ -z "$MODEL" ]; then
  echo "CANNOT LAUNCH: no ollama model available"
else
  export ANTHROPIC_BASE_URL="http://127.0.0.1:11434"
  export ANTHROPIC_AUTH_TOKEN="ollama-local-probe"
  export ANTHROPIC_API_KEY="ollama-local-probe"
  export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
  echo "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL"
  echo "CLAUDE_MODEL=$MODEL"
  echo "\$ claude -p --model $MODEL 'Reply with exactly: CLAUDE_OLLAMA_OK'"
  CLAUDE_OUT=$("$CLAUDE" -p --model "$MODEL" \
      "Reply with exactly: CLAUDE_OLLAMA_OK" 2>&1)
  CLAUDE_RC=$?
  echo "exit_code=$CLAUDE_RC"
  echo "--- claude output (first 1200 chars) ---"
  printf '%s\n' "$CLAUDE_OUT" | head -c 1200
  echo
  echo "claude_launch_exit=$CLAUDE_RC"
  case "$CLAUDE_OUT" in
    *CLAUDE_OLLAMA_OK*) echo "RESULT: SUCCESS — claude answered via the local Ollama model" ;;
    *) echo "RESULT: NO-CONFIRMATION — claude did not echo the sentinel" ;;
  esac
fi

echo "=== STEP 4: CONCLUSION ==="
echo "claude_present=$([ -n "${CLAUDE:-}" ] && echo yes || echo no)"
echo "ollama_models=$MODEL"
echo "anthropic_surface_http=${ANTHROPIC_STATUS:-n/a}"
echo "transcript_file=$OUT"
echo "=== END ==="
'''


async def main() -> None:
    from lucent.db.pool import init_db
    from lucent.db.requests import RequestRepository

    pool = await init_db(os.environ.get("DATABASE_URL"), run_migrations=False)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT organization_id::text AS org_id, user_id::text AS user_id
               FROM llm_sessions WHERE id = $1::uuid""",
            ORIGIN_SESSION,
        )
    org_id, user_id = row["org_id"], row["user_id"]
    repo = RequestRepository(pool)

    request = await repo.create_request_with_tasks(
        org_id=org_id,
        title="Launch claude against a local Ollama model on the Mac remote",
        description=DESCRIPTION,
        source="user",
        priority="high",
        created_by=user_id,
        target_paths=[],
        task_specs=[
            {
                "title": "Run claude against local Ollama and report the answer",
                "description": (
                    "Run on the kyles-macbook remote. This is an EXECUTION task, "
                    "not a discovery task: it must actually invoke the CLI.\n\n"
                    "Do not use the oclauda backend or its container.\n\n"
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
            "UPDATE requests SET origin_session_id = $2::uuid WHERE id = $1::uuid",
            request_id,
            ORIGIN_SESSION,
        )
        await conn.execute(
            """INSERT INTO llm_session_requests (session_id, request_id, relation)
               VALUES ($1::uuid, $2::uuid, 'created')
               ON CONFLICT DO NOTHING""",
            ORIGIN_SESSION,
            request_id,
        )
    print(json.dumps({"request_id": request_id, "status": request.get("status")}, indent=2))


asyncio.run(main())
