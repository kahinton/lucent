#!/bin/sh
# OpenBao initialization + unseal script for Lucent local development.
#
# Handles:
#   - First-time init (generates root token + unseal key, persists to /shared)
#   - Auto-unseal on subsequent starts (reads persisted unseal key)
#   - Idempotent engine + key + policy setup
#
# Designed for file-backed OpenBao in local dev. Production should use
# Shamir key splits with manual unseal or cloud auto-unseal — see
# docker/openbao-prod-config.hcl.
set -eu

VAULT_ADDR="${VAULT_ADDR:-http://openbao:8200}"
export VAULT_ADDR

INIT_STATE_FILE="/shared/openbao-init.json"
SHARED_TOKEN_FILE="/shared/vault-token"

mkdir -p /shared

echo "Waiting for OpenBao at ${VAULT_ADDR}..."
i=0
while [ $i -lt 60 ]; do
  status_code=$(curl -s -o /dev/null -w '%{http_code}' \
    "${VAULT_ADDR}/v1/sys/health?uninitcode=200&sealedcode=200&standbyok=true" 2>/dev/null || echo "000")
  if [ "${status_code}" != "000" ] && [ "${status_code}" != "" ]; then
    break
  fi
  sleep 1
  i=$((i + 1))
done
if [ $i -ge 60 ]; then
  echo "ERROR: OpenBao did not become reachable within 60s" >&2
  exit 1
fi
echo "OpenBao is reachable."

# ---------------------------------------------------------------------------
# Initialize (first boot) or load existing keys
# ---------------------------------------------------------------------------

init_status=$(curl -sf "${VAULT_ADDR}/v1/sys/init" | sed -n 's/.*"initialized":\([a-z]*\).*/\1/p')

if [ "${init_status}" = "false" ]; then
  echo "OpenBao is not initialized. Initializing with dev-grade (1/1) unseal config..."
  init_response=$(curl -sf -X POST "${VAULT_ADDR}/v1/sys/init" \
    -d '{"secret_shares":1,"secret_threshold":1}')
  if [ -z "${init_response}" ]; then
    echo "ERROR: OpenBao init returned empty response" >&2
    exit 1
  fi
  echo "${init_response}" > "${INIT_STATE_FILE}"
  chmod 600 "${INIT_STATE_FILE}"
  echo "Stored init state at ${INIT_STATE_FILE} (dev only)."
else
  if [ ! -f "${INIT_STATE_FILE}" ]; then
    echo "ERROR: OpenBao is initialized but ${INIT_STATE_FILE} is missing." >&2
    echo "       Cannot auto-unseal without the stored unseal key." >&2
    echo "       To reset: docker volume rm hindsight_openbao_data hindsight_openbao_shared" >&2
    exit 1
  fi
  echo "OpenBao already initialized. Loading stored unseal/root credentials..."
fi

ROOT_TOKEN=$(sed -n 's/.*"root_token":"\([^"]*\)".*/\1/p' "${INIT_STATE_FILE}")
UNSEAL_KEY=$(sed -n 's/.*"keys_base64":\["\([^"]*\)"\].*/\1/p' "${INIT_STATE_FILE}")

if [ -z "${ROOT_TOKEN}" ] || [ -z "${UNSEAL_KEY}" ]; then
  echo "ERROR: Could not parse root_token or unseal key from ${INIT_STATE_FILE}" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Unseal if needed
# ---------------------------------------------------------------------------

sealed=$(curl -sf "${VAULT_ADDR}/v1/sys/seal-status" | sed -n 's/.*"sealed":\([a-z]*\).*/\1/p')
if [ "${sealed}" = "true" ]; then
  echo "OpenBao is sealed. Unsealing..."
  curl -sf -X POST "${VAULT_ADDR}/v1/sys/unseal" \
    -d "{\"key\":\"${UNSEAL_KEY}\"}" > /dev/null
  sealed_after=$(curl -sf "${VAULT_ADDR}/v1/sys/seal-status" | sed -n 's/.*"sealed":\([a-z]*\).*/\1/p')
  if [ "${sealed_after}" = "true" ]; then
    echo "ERROR: Unseal failed" >&2
    exit 1
  fi
  echo "OpenBao unsealed."
fi

VAULT_TOKEN="${ROOT_TOKEN}"
export VAULT_TOKEN

# ---------------------------------------------------------------------------
# Idempotent engine + key + policy setup
# ---------------------------------------------------------------------------

# KV v2 at secret/ (may already exist; ignore errors)
curl -sf -X POST "${VAULT_ADDR}/v1/sys/mounts/secret" \
  -H "X-Vault-Token: ${VAULT_TOKEN}" \
  -d '{"type":"kv","options":{"version":"2"}}' > /dev/null 2>&1 || true

# Transit engine
curl -sf -X POST "${VAULT_ADDR}/v1/sys/mounts/transit" \
  -H "X-Vault-Token: ${VAULT_TOKEN}" \
  -d '{"type":"transit"}' > /dev/null 2>&1 || true

# Keys for secrets and credentials — safe to re-request; Vault returns 400 if exists
curl -sf -X POST "${VAULT_ADDR}/v1/transit/keys/lucent-secrets" \
  -H "X-Vault-Token: ${VAULT_TOKEN}" > /dev/null 2>&1 || true
curl -sf -X POST "${VAULT_ADDR}/v1/transit/keys/lucent-credentials" \
  -H "X-Vault-Token: ${VAULT_TOKEN}" > /dev/null 2>&1 || true

# Policy
curl -sf -X PUT "${VAULT_ADDR}/v1/sys/policies/acl/lucent-policy" \
  -H "X-Vault-Token: ${VAULT_TOKEN}" \
  -d '{
  "policy": "path \"secret/data/lucent/*\" {\n  capabilities = [\"read\", \"create\", \"update\", \"delete\"]\n}\npath \"secret/metadata/lucent/*\" {\n  capabilities = [\"list\", \"read\", \"delete\"]\n}\npath \"transit/encrypt/lucent-secrets\" {\n  capabilities = [\"update\"]\n}\npath \"transit/decrypt/lucent-secrets\" {\n  capabilities = [\"update\"]\n}\npath \"transit/encrypt/lucent-credentials\" {\n  capabilities = [\"update\"]\n}\npath \"transit/decrypt/lucent-credentials\" {\n  capabilities = [\"update\"]\n}"
}' > /dev/null

# ---------------------------------------------------------------------------
# Write the root token to /shared so other services can read it.
#
# In dev we use the root token directly — lucent-policy is created above but
# several Lucent subsystems (daemon, sandbox bridge) need broader access to
# sys/ paths and the policy would need extending. For production, the policy
# approach is used; see docker-compose.prod.yml.
# ---------------------------------------------------------------------------

echo "${ROOT_TOKEN}" > "${SHARED_TOKEN_FILE}"
chmod 600 "${SHARED_TOKEN_FILE}"
echo "Root token written to ${SHARED_TOKEN_FILE}"

echo "OpenBao initialization complete."
