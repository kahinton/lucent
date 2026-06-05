# OpenBao dev configuration — file-backed storage for local development.
#
# This is NOT the production config (see docker/openbao-prod-config.hcl).
# It uses file storage with a persistent Docker volume so the transit key
# and stored credentials survive container restarts — fixing the biggest
# ergonomic issue with running `-dev` mode locally (which is memory-only
# and wipes everything on restart).
#
# Dev-specific trade-offs:
#   - Single unseal key with a threshold of 1 (production should use >=3 with Shamir split)
#   - Unseal key and root token auto-persisted to /shared/openbao-init.json so the
#     init container can auto-unseal on restart. Do NOT use this file outside local dev.

storage "file" {
  # Use /openbao/file which is pre-created and owned by the openbao user in
  # the image. (If we mount to /openbao/data the named volume comes up as
  # root-owned and OpenBao's file backend can't write to it.)
  path = "/openbao/file"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

# Container-only; IPC_LOCK is granted in docker-compose.
# (OpenBao 2.x dropped the disable_mlock config option — memlock is no longer
# used, so we don't need to set anything here.)

api_addr = "http://openbao:8200"
cluster_addr = "http://openbao:8201"

ui = true
