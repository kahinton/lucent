#!/bin/sh
# OpenBao initialization script for Lucent local development.
# Idempotent — safe to run multiple times.
set -e

VAULT_ADDR="${VAULT_ADDR:-http://openbao:8200}"
VAULT_TOKEN="${VAULT_TOKEN:-change-me-insecure-dev-root-token}"
export VAULT_ADDR VAULT_TOKEN

echo "Waiting for OpenBao at ${VAULT_ADDR}..."
until curl -sf "${VAULT_ADDR}/v1/sys/health" > /dev/null 2>&1; do
  sleep 1
done
echo "OpenBao is ready."

# Enable KV v2 at secret/ (already enabled in dev mode — ignore error)
curl -sf -X POST "${VAULT_ADDR}/v1/sys/mounts/secret" \
  -H "X-Vault-Token: ${VAULT_TOKEN}" \
  -d '{"type":"kv","options":{"version":"2"}}' > /dev/null 2>&1 || true

# Enable Transit engine at transit/
curl -sf -X POST "${VAULT_ADDR}/v1/sys/mounts/transit" \
  -H "X-Vault-Token: ${VAULT_TOKEN}" \
  -d '{"type":"transit"}' > /dev/null 2>&1 || true

# Create Transit encryption key
curl -sf -X POST "${VAULT_ADDR}/v1/transit/keys/lucent-secrets" \
  -H "X-Vault-Token: ${VAULT_TOKEN}" > /dev/null 2>&1 || true

# Create lucent-policy
curl -sf -X PUT "${VAULT_ADDR}/v1/sys/policies/acl/lucent-policy" \
  -H "X-Vault-Token: ${VAULT_TOKEN}" \
  -d '{
  "policy": "path \"secret/data/lucent/*\" {\n  capabilities = [\"read\", \"create\", \"update\", \"delete\"]\n}\npath \"secret/metadata/lucent/*\" {\n  capabilities = [\"list\", \"read\", \"delete\"]\n}\npath \"transit/encrypt/lucent-secrets\" {\n  capabilities = [\"update\"]\n}\npath \"transit/decrypt/lucent-secrets\" {\n  capabilities = [\"update\"]\n}"
}' > /dev/null

# Create a token with lucent-policy and write it to shared volume
RESPONSE=$(curl -sf -X POST "${VAULT_ADDR}/v1/auth/token/create" \
  -H "X-Vault-Token: ${VAULT_TOKEN}" \
  -d '{"policies":["lucent-policy"],"ttl":"768h","renewable":true}')

CLIENT_TOKEN=$(echo "${RESPONSE}" | sed -n 's/.*"client_token":"\([^"]*\)".*/\1/p')

if [ -n "${CLIENT_TOKEN}" ]; then
  echo "Lucent policy token created successfully (not printed for security)"
  # Write to shared volume so Lucent can read it
  mkdir -p /shared
  echo "${CLIENT_TOKEN}" > /shared/vault-token
  chmod 600 /shared/vault-token
  echo "Token written to /shared/vault-token"
else
  echo "WARNING: Could not create policy token, using VAULT_TOKEN fallback for dev"
  echo "WARNING: The fallback token has root privileges — do NOT use in production"
  mkdir -p /shared
  echo "${VAULT_TOKEN}" > /shared/vault-token
  chmod 600 /shared/vault-token
fi

echo "OpenBao initialization complete."
