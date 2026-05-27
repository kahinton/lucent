# OpenBao production configuration for Lucent
# Used by docker-compose.prod.yml

storage "file" {
  path = "/openbao/data"
}

listener "tcp" {
  # Listen only on localhost within the container.
  # Other services reach OpenBao via Docker internal network.
  address     = "0.0.0.0:8200"
  tls_disable = true  # TLS is terminated at the reverse proxy / load balancer

  # In production with direct TLS:
  # tls_cert_file = "/openbao/tls/server.crt"
  # tls_key_file  = "/openbao/tls/server.key"
  # tls_disable   = false
}

# Disable mlock if running in a container without IPC_LOCK capability
disable_mlock = false

# API address for client connections
api_addr = "http://openbao:8200"

# UI is disabled in production by default
ui = false

# Telemetry (optional)
# telemetry {
#   prometheus_retention_time = "30s"
#   disable_hostname = true
# }
