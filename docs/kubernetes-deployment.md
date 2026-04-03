# Kubernetes Deployment Guide

This guide covers deploying Lucent on Kubernetes using the official Helm chart located at
`deploy/helm/lucent/`. For declarative management via the Lucent Kubernetes Operator, see
[operator-guide.md](operator-guide.md).

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Quick Start](#quick-start)
3. [Configuration Reference](#configuration-reference)
4. [Common Configurations](#common-configurations)
5. [TLS Configuration](#tls-configuration)
6. [Database Management](#database-management)
7. [Monitoring & Observability](#monitoring--observability)
8. [Upgrading](#upgrading)
9. [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Requirement | Minimum Version | Notes |
|---|---|---|
| Kubernetes | 1.25 | 1.28+ recommended for stable HPA v2 |
| Helm | 3.10 | `helm version` to verify |
| kubectl | matching cluster | Must have cluster-admin or equivalent for initial install |
| cert-manager | 1.13 | Optional — only required for automated TLS provisioning |
| Prometheus Operator | 0.65 | Optional — required for `observability.prometheus.serviceMonitor` |

**Helm plugin (optional but recommended):**

```bash
helm plugin install https://github.com/databus23/helm-diff
```

**Add the Bitnami repository** (required for the embedded PostgreSQL dependency):

```bash
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update
```

---

## Quick Start

The fastest way to run Lucent with the embedded PostgreSQL database and OpenBao vault.

### 1. Clone the repository and update dependencies

```bash
git clone https://github.com/your-org/lucent.git
cd lucent
helm dependency update deploy/helm/lucent
```

### 2. Create a namespace

```bash
kubectl create namespace lucent
```

### 3. Install with embedded services

```bash
helm install lucent deploy/helm/lucent \
  --namespace lucent \
  --set server.env.LUCENT_SIGNING_SECRET="$(openssl rand -hex 32)" \
  --set server.env.LUCENT_SECRET_KEY="$(openssl rand -hex 32)"
```

### 4. Access the server

```bash
kubectl -n lucent port-forward svc/lucent-server 8766:8766
```

Open [http://localhost:8766](http://localhost:8766) in your browser.

### 5. Check health

```bash
curl http://localhost:8766/api/health
# {"status": "healthy"}
```

---

## Configuration Reference

All parameters are set via `--set` flags or `values.yaml` overrides. The chart ships with
`values-dev.yaml`, `values-staging.yaml`, and `values-prod.yaml` examples under
`deploy/helm/lucent/`.

### Server

| Parameter | Type | Default | Description |
|---|---|---|---|
| `server.replicaCount` | int | `1` | Number of lucent-server pods |
| `server.image.repository` | string | `lucent` | Container image repository |
| `server.image.tag` | string | `0.2.0` | Container image tag |
| `server.image.pullPolicy` | string | `IfNotPresent` | Image pull policy |
| `server.service.type` | string | `ClusterIP` | Kubernetes service type |
| `server.service.port` | int | `8766` | Service port |
| `server.health.path` | string | `/api/health` | Liveness and readiness probe path |
| `server.resources.requests.cpu` | string | `250m` | CPU request |
| `server.resources.requests.memory` | string | `512Mi` | Memory request |
| `server.resources.limits.cpu` | string | `1000m` | CPU limit |
| `server.resources.limits.memory` | string | `1Gi` | Memory limit |
| `server.env.LUCENT_HOST` | string | `0.0.0.0` | Bind address |
| `server.env.LUCENT_PORT` | string | `8766` | Server listen port |
| `server.env.LUCENT_MODE` | string | `personal` | Deployment mode: `personal` or `team` |
| `server.env.LUCENT_SIGNING_SECRET` | string | `""` | Session signing secret — **must be set explicitly in production** |
| `server.env.LUCENT_SECRET_KEY` | string | `""` | Fernet key for builtin secret provider |
| `server.env.LUCENT_SECURE_COOKIES` | string | `true` | Enforce Secure flag on session cookies |
| `server.env.LUCENT_SESSION_TTL_HOURS` | string | `24` | Session token lifetime |
| `server.env.LUCENT_CORS_ORIGINS` | string | `""` | Comma-separated allowed CORS origins |
| `server.env.LUCENT_RATE_LIMIT_PER_MINUTE` | string | `100` | API rate limit per key/IP |
| `server.env.LUCENT_LOGIN_RATE_LIMIT` | string | `5` | Max login attempts per minute |
| `server.env.LUCENT_TRUSTED_PROXIES` | string | `""` | CIDR ranges of trusted reverse proxies |
| `server.env.LUCENT_LOG_LEVEL` | string | `INFO` | Log level |
| `server.env.LUCENT_LOG_FORMAT` | string | `human` | Log format: `human` or `json` |
| `global.imageRegistry` | string | `""` | Override image registry for all images |
| `global.imagePullSecrets` | list | `[]` | Image pull secrets for private registries |

### Database

| Parameter | Type | Default | Description |
|---|---|---|---|
| `database.embedded` | bool | `true` | Deploy embedded PostgreSQL StatefulSet |
| `database.image.repository` | string | `postgres` | PostgreSQL image (embedded mode) |
| `database.image.tag` | string | `16-alpine` | PostgreSQL image tag |
| `database.persistence.enabled` | bool | `true` | Enable PVC for embedded DB |
| `database.persistence.size` | string | `10Gi` | PVC size |
| `database.persistence.storageClass` | string | `""` | Storage class (uses default if empty) |
| `database.external.host` | string | `""` | External PostgreSQL host |
| `database.external.port` | int | `5432` | External PostgreSQL port |
| `database.external.secretRef` | string | `""` | Secret containing `DATABASE_URL` |
| `postgresql.auth.database` | string | `lucent` | Database name (embedded mode) |
| `postgresql.auth.username` | string | `lucent` | Database username (embedded mode) |
| `postgresql.auth.existingSecret` | string | `""` | Existing secret for DB credentials |
| `postgresql.primary.persistence.size` | string | `10Gi` | Bitnami subchart PVC size |

### OpenBao

| Parameter | Type | Default | Description |
|---|---|---|---|
| `openbao.embedded` | bool | `true` | Deploy embedded OpenBao StatefulSet |
| `openbao.image.repository` | string | `openbao/openbao` | OpenBao image |
| `openbao.image.tag` | string | `2.1.0` | OpenBao image tag |
| `openbao.persistence.enabled` | bool | `true` | Enable PVC for embedded OpenBao |
| `openbao.persistence.size` | string | `5Gi` | PVC size |
| `openbao.persistence.storageClass` | string | `""` | Storage class |
| `openbao.external.address` | string | `""` | External Vault/OpenBao API URL |
| `openbao.external.secretRef` | string | `""` | Secret containing `VAULT_ADDR` and `VAULT_TOKEN` |

### LLM

| Parameter | Type | Default | Description |
|---|---|---|---|
| `llm.engine` | string | `copilot` | LLM backend: `copilot` or `langchain` |
| `llm.daemonModel` | string | `claude-opus-4.6` | Model for daemon cognitive loop |
| `llm.providers.anthropic.secretRef` | string | `""` | Secret containing `ANTHROPIC_API_KEY` |
| `llm.providers.openai.secretRef` | string | `""` | Secret containing `OPENAI_API_KEY` |
| `llm.providers.google.secretRef` | string | `""` | Secret containing `GOOGLE_API_KEY` |

### Ollama

| Parameter | Type | Default | Description |
|---|---|---|---|
| `ollama.enabled` | bool | `false` | Enable Ollama for local inference |
| `ollama.mode` | string | `sidecar` | Deployment mode: `sidecar` or `daemonset` |
| `ollama.image.repository` | string | `ollama/ollama` | Ollama image |
| `ollama.image.tag` | string | `latest` | Ollama image tag |
| `ollama.gpu.enabled` | bool | `false` | Request GPU resources (`nvidia.com/gpu`) |
| `ollama.gpu.count` | int | `1` | Number of GPUs to request per pod |
| `ollama.nodeSelector` | object | `{}` | Node selector for GPU nodes |
| `ollama.tolerations` | list | `[]` | Tolerations for GPU taints |

### Ingress

| Parameter | Type | Default | Description |
|---|---|---|---|
| `ingress.enabled` | bool | `false` | Create an Ingress resource |
| `ingress.className` | string | `""` | Ingress class (`nginx`, `traefik`, `alb`) |
| `ingress.annotations` | object | `{}` | Annotations (e.g., cert-manager, ALB) |
| `ingress.hosts[].host` | string | `lucent.local` | Hostname |
| `ingress.hosts[].paths[].path` | string | `/` | Path |
| `ingress.hosts[].paths[].pathType` | string | `Prefix` | Path type |
| `ingress.tls` | list | `[]` | TLS configuration blocks |

### Autoscaling

| Parameter | Type | Default | Description |
|---|---|---|---|
| `autoscaling.enabled` | bool | `false` | Enable HorizontalPodAutoscaler |
| `autoscaling.minReplicas` | int | `2` | Minimum replicas |
| `autoscaling.maxReplicas` | int | `10` | Maximum replicas |
| `autoscaling.targetCPU` | int | `80` | Target CPU utilization (%) |
| `podDisruptionBudget.enabled` | bool | `false` | Create PodDisruptionBudget |
| `podDisruptionBudget.minAvailable` | int | `1` | Minimum available pods during disruption |

### Networking

| Parameter | Type | Default | Description |
|---|---|---|---|
| `networkPolicy.enabled` | bool | `false` | Deploy NetworkPolicies (default-deny + allow rules) |
| `serviceAccount.create` | bool | `true` | Create a ServiceAccount |
| `serviceAccount.name` | string | `""` | ServiceAccount name (auto-generated if empty) |
| `serviceAccount.annotations` | object | `{}` | ServiceAccount annotations (e.g., IRSA) |

### Observability

| Parameter | Type | Default | Description |
|---|---|---|---|
| `observability.enabled` | bool | `false` | Master switch for all observability features |
| `observability.otelCollector.enabled` | bool | `false` | Deploy OTEL collector sidecar |
| `observability.prometheus.serviceMonitor` | bool | `false` | Create Prometheus ServiceMonitor |
| `observability.grafana.dashboards` | bool | `false` | Create Grafana dashboard ConfigMap |
| `server.env.OTEL_ENABLED` | string | `false` | Enable OTEL tracing/metrics in the server |
| `server.env.OTEL_EXPORTER_OTLP_ENDPOINT` | string | `http://otel-collector:4317` | OTLP gRPC endpoint |
| `server.env.OTEL_SERVICE_NAME` | string | `lucent` | Service name attribute |

### Daemon

| Parameter | Type | Default | Description |
|---|---|---|---|
| `daemon.enabled` | bool | `true` | Enable the autonomous daemon process |
| `daemon.model` | string | `claude-opus-4.6` | Model for daemon cognitive loop |
| `daemon.maxConcurrentSessions` | int | `3` | Max parallel sub-agent sessions |
| `daemon.env.LUCENT_DAEMON_INTERVAL` | string | `15` | Minutes between cognitive cycles |
| `daemon.env.LUCENT_STALE_HEARTBEAT_MINUTES` | string | `30` | Minutes before a claimed task is considered stale |

---

## Common Configurations

### Single-Node Development (minikube / kind)

Uses embedded PostgreSQL and OpenBao with minimal resources. Suitable for local development
and experimentation.

```bash
# Start a local cluster
minikube start --memory=4096 --cpus=4
# or: kind create cluster --name lucent-dev

# Update chart dependencies
helm dependency update deploy/helm/lucent

# Install using the bundled dev values
helm install lucent-dev deploy/helm/lucent \
  --namespace lucent-dev \
  --create-namespace \
  -f deploy/helm/lucent/values-dev.yaml \
  --set server.env.LUCENT_SIGNING_SECRET="dev-secret-do-not-use-in-prod" \
  --set server.env.LUCENT_SECRET_KEY="dev-fernet-key-32-chars-minimum!!"

# Port-forward and verify
kubectl -n lucent-dev port-forward svc/lucent-dev-server 8766:8766 &
curl http://localhost:8766/api/health
```

---

### Production with External PostgreSQL

Connects to an existing managed PostgreSQL instance (e.g., RDS, Cloud SQL, Azure Database).

**Step 1 — Create the database secret:**

```bash
kubectl create secret generic lucent-db-credentials \
  --namespace lucent \
  --from-literal=DATABASE_URL="postgresql://lucent:STRONG_PASSWORD@pg.example.com:5432/lucent"
```

**Step 2 — Create the signing secret:**

```bash
kubectl create secret generic lucent-app-secrets \
  --namespace lucent \
  --from-literal=LUCENT_SIGNING_SECRET="$(openssl rand -hex 32)" \
  --from-literal=LUCENT_SECRET_KEY="$(openssl rand -hex 32)"
```

**Step 3 — Install:**

```bash
helm install lucent deploy/helm/lucent \
  --namespace lucent \
  --create-namespace \
  -f deploy/helm/lucent/values-prod.yaml \
  --set database.embedded=false \
  --set database.external.host=pg.example.com \
  --set database.external.port=5432 \
  --set database.external.secretRef=lucent-db-credentials \
  --set server.env.LUCENT_MODE=team \
  --set server.env.LUCENT_LICENSE_KEY="YOUR_LICENSE_KEY"
```

---

### Production with HPA and Observability

Full production setup: 3 replicas, autoscaling, PDB, network policies, Prometheus monitoring,
and Grafana dashboards.

```bash
helm install lucent deploy/helm/lucent \
  --namespace lucent \
  --create-namespace \
  -f deploy/helm/lucent/values-prod.yaml \
  --set server.replicaCount=3 \
  --set autoscaling.enabled=true \
  --set autoscaling.minReplicas=3 \
  --set autoscaling.maxReplicas=12 \
  --set autoscaling.targetCPU=70 \
  --set podDisruptionBudget.enabled=true \
  --set podDisruptionBudget.minAvailable=2 \
  --set networkPolicy.enabled=true \
  --set observability.enabled=true \
  --set observability.prometheus.serviceMonitor=true \
  --set observability.grafana.dashboards=true \
  --set server.env.OTEL_ENABLED="true" \
  --set server.env.OTEL_ENVIRONMENT=production \
  --set database.embedded=false \
  --set database.external.secretRef=lucent-db-credentials
```

Verify HPA is active:

```bash
kubectl -n lucent get hpa
# NAME              REFERENCE                  TARGETS   MINPODS   MAXPODS   REPLICAS
# lucent-server     Deployment/lucent-server   42%/70%   3         12        3
```

---

### Air-Gapped Deployment with Ollama

For environments without internet access, Lucent can use Ollama for fully local inference.
GPU nodes are required for performant inference.

**Step 1 — Mirror images to your private registry:**

```bash
REGISTRY=registry.internal.example.com

for IMAGE in lucent:0.2.0 postgres:16-alpine openbao/openbao:2.1.0 ollama/ollama:latest; do
  docker pull $IMAGE
  docker tag $IMAGE $REGISTRY/$IMAGE
  docker push $REGISTRY/$IMAGE
done
```

**Step 2 — Create image pull secret:**

```bash
kubectl create secret docker-registry regcred \
  --namespace lucent \
  --docker-server=registry.internal.example.com \
  --docker-username=robot \
  --docker-password=TOKEN
```

**Step 3 — Install with Ollama DaemonSet on GPU nodes:**

```bash
helm install lucent deploy/helm/lucent \
  --namespace lucent \
  --create-namespace \
  --set global.imageRegistry=registry.internal.example.com \
  --set global.imagePullSecrets[0].name=regcred \
  --set llm.engine=langchain \
  --set llm.daemonModel=qwen2.5-coder-7b \
  --set daemon.model=qwen2.5-coder-7b \
  --set server.env.OLLAMA_HOST="http://ollama:11434" \
  --set ollama.enabled=true \
  --set ollama.mode=daemonset \
  --set ollama.gpu.enabled=true \
  --set ollama.gpu.count=1 \
  --set 'ollama.nodeSelector.nvidia\.com/gpu=present' \
  --set 'ollama.tolerations[0].key=nvidia.com/gpu' \
  --set 'ollama.tolerations[0].operator=Exists' \
  --set 'ollama.tolerations[0].effect=NoSchedule'
```

**Step 4 — Pre-pull models into Ollama (first boot):**

```bash
OLLAMA_POD=$(kubectl -n lucent get pod -l app.kubernetes.io/component=ollama -o jsonpath='{.items[0].metadata.name}')
kubectl -n lucent exec $OLLAMA_POD -- ollama pull qwen2.5-coder:7b
```

---

### Multi-Tenant Setup

Run isolated Lucent instances per team, each in its own namespace with shared infrastructure.

```bash
# Create per-team namespace and database secret
for TEAM in team-alpha team-beta; do
  kubectl create namespace lucent-$TEAM

  kubectl create secret generic lucent-db-credentials \
    --namespace lucent-$TEAM \
    --from-literal=DATABASE_URL="postgresql://lucent_$TEAM:PASSWORD@pg.example.com:5432/lucent_$TEAM"

  helm install lucent-$TEAM deploy/helm/lucent \
    --namespace lucent-$TEAM \
    -f deploy/helm/lucent/values-prod.yaml \
    --set database.embedded=false \
    --set database.external.secretRef=lucent-db-credentials \
    --set ingress.enabled=true \
    --set ingress.className=nginx \
    --set ingress.hosts[0].host=$TEAM.lucent.example.com \
    --set "ingress.tls[0].secretName=lucent-$TEAM-tls" \
    --set "ingress.tls[0].hosts[0]=$TEAM.lucent.example.com" \
    --set networkPolicy.enabled=true
done
```

Each namespace is fully isolated — no shared state, no cross-namespace network traffic.

---

## TLS Configuration

### Automated TLS with cert-manager

Install cert-manager first if not already present:

```bash
helm repo add jetstack https://charts.jetstack.io
helm repo update
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --set crds.enabled=true
```

Create a ClusterIssuer for Let's Encrypt:

```yaml
# letsencrypt-issuer.yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: admin@example.com
    privateKeySecretRef:
      name: letsencrypt-prod-key
    solvers:
      - http01:
          ingress:
            ingressClassName: nginx
```

```bash
kubectl apply -f letsencrypt-issuer.yaml
```

Install Lucent with cert-manager annotations:

```bash
helm install lucent deploy/helm/lucent \
  --namespace lucent \
  --create-namespace \
  -f deploy/helm/lucent/values-prod.yaml \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set 'ingress.annotations.cert-manager\.io/cluster-issuer=letsencrypt-prod' \
  --set ingress.hosts[0].host=lucent.example.com \
  --set 'ingress.hosts[0].paths[0].path=/' \
  --set 'ingress.hosts[0].paths[0].pathType=Prefix' \
  --set 'ingress.tls[0].secretName=lucent-tls' \
  --set 'ingress.tls[0].hosts[0]=lucent.example.com'
```

cert-manager will automatically provision and renew the certificate.

### Manual Certificate Secrets

If you manage certificates yourself (e.g., internal CA, wildcard cert):

```bash
kubectl create secret tls lucent-tls \
  --namespace lucent \
  --cert=tls.crt \
  --key=tls.key
```

Then reference it in your values:

```yaml
# values-custom-tls.yaml
ingress:
  enabled: true
  className: nginx
  hosts:
    - host: lucent.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: lucent-tls
      hosts:
        - lucent.example.com
```

```bash
helm upgrade lucent deploy/helm/lucent \
  --namespace lucent \
  -f deploy/helm/lucent/values-prod.yaml \
  -f values-custom-tls.yaml
```

### AWS ALB with ACM

```yaml
ingress:
  enabled: true
  className: alb
  annotations:
    kubernetes.io/ingress.class: alb
    alb.ingress.kubernetes.io/scheme: internet-facing
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:us-east-1:123456789:certificate/abc-def
    alb.ingress.kubernetes.io/listen-ports: '[{"HTTP": 80}, {"HTTPS": 443}]'
    alb.ingress.kubernetes.io/ssl-redirect: "443"
```

---

## Database Management

### External PostgreSQL Setup

Lucent requires a PostgreSQL 14+ database. Create the database and user before installing:

```sql
CREATE DATABASE lucent;
CREATE USER lucent WITH ENCRYPTED PASSWORD 'STRONG_PASSWORD';
GRANT ALL PRIVILEGES ON DATABASE lucent TO lucent;
```

Store the connection URL in a Kubernetes secret:

```bash
kubectl create secret generic lucent-db-credentials \
  --namespace lucent \
  --from-literal=DATABASE_URL="postgresql://lucent:STRONG_PASSWORD@pg.example.com:5432/lucent"
```

Reference it in the Helm values:

```yaml
database:
  embedded: false
  external:
    host: pg.example.com
    port: 5432
    secretRef: lucent-db-credentials
```

For connection pooling with PgBouncer (recommended for HPA deployments with many replicas):

```bash
DATABASE_URL="postgresql://lucent:PASSWORD@pgbouncer.example.com:5432/lucent?sslmode=require"
```

### Embedded PostgreSQL Backup

When using `database.embedded=true`, back up the embedded PostgreSQL PVC with `pg_dump`:

```bash
DB_POD=$(kubectl -n lucent get pod -l app.kubernetes.io/component=db -o jsonpath='{.items[0].metadata.name}')

kubectl -n lucent exec $DB_POD -- \
  pg_dump -U lucent lucent | gzip > lucent-backup-$(date +%Y%m%d).sql.gz
```

Restore from backup:

```bash
gunzip -c lucent-backup-20260321.sql.gz | \
  kubectl -n lucent exec -i $DB_POD -- psql -U lucent lucent
```

**Automated backups** — the Kubernetes Operator manages backup CronJobs. See
[operator-guide.md](operator-guide.md) for the `LucentInstance.spec.backup` configuration.

### Database Migrations

Lucent applies schema migrations automatically at startup. When upgrading, the new server pod
runs migrations before accepting traffic due to the rolling update strategy and readiness probe.

To run migrations manually (e.g., before a zero-downtime canary):

```bash
kubectl -n lucent run lucent-migrate --rm -i --restart=Never \
  --image=lucent:0.3.0 \
  --env="DATABASE_URL=$(kubectl -n lucent get secret lucent-db-credentials -o jsonpath='{.data.DATABASE_URL}' | base64 -d)" \
  -- python -m lucent.migrate
```

> **Warning:** Schema rollback is only supported for migrations that ship a paired
> `.down.sql` file and are not marked `-- lucent: rollback=irreversible`. Always
> back up before upgrading to a new major version.

---

## Monitoring & Observability

### Prometheus ServiceMonitor

Enable the ServiceMonitor when Prometheus Operator is installed:

```bash
helm upgrade lucent deploy/helm/lucent \
  --namespace lucent \
  --reuse-values \
  --set observability.enabled=true \
  --set observability.prometheus.serviceMonitor=true \
  --set server.env.OTEL_ENABLED="true"
```

Verify the ServiceMonitor is picked up:

```bash
kubectl -n lucent get servicemonitor
kubectl -n lucent describe servicemonitor lucent
```

The ServiceMonitor exposes metrics on `http://<pod>:8766/metrics`. Confirm Prometheus is
scraping:

```bash
# In Prometheus UI, search for: lucent_
```

### Grafana Dashboards

The chart can deploy a pre-built Grafana dashboard ConfigMap:

```bash
helm upgrade lucent deploy/helm/lucent \
  --namespace lucent \
  --reuse-values \
  --set observability.grafana.dashboards=true
```

The ConfigMap is labeled `grafana_dashboard: "1"` so Grafana's sidecar picks it up
automatically when the Grafana Helm chart is installed with `sidecar.dashboards.enabled=true`.

To import manually:

```bash
kubectl -n lucent get configmap lucent-grafana-dashboard -o jsonpath='{.data.dashboard\.json}' > lucent-dashboard.json
# Import lucent-dashboard.json via Grafana UI → Dashboards → Import
```

### OpenTelemetry Collector

Enable the OTEL collector sidecar alongside the server:

```bash
helm upgrade lucent deploy/helm/lucent \
  --namespace lucent \
  --reuse-values \
  --set observability.otelCollector.enabled=true \
  --set server.env.OTEL_ENABLED="true" \
  --set server.env.OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4317" \
  --set server.env.OTEL_SERVICE_NAME="lucent" \
  --set server.env.OTEL_ENVIRONMENT="production"
```

To forward traces to Jaeger or a managed backend, override the OTEL collector ConfigMap after
installation:

```bash
kubectl -n lucent get configmap lucent-otel-collector-config -o yaml > otel-config.yaml
# Edit otel-config.yaml to add your exporters
kubectl -n lucent apply -f otel-config.yaml
kubectl -n lucent rollout restart deployment/lucent-server
```

### Key Metrics to Monitor

| Metric | Alert Threshold | Description |
|---|---|---|
| `lucent_active_sessions` | > `maxConcurrentSessions` | Daemon sessions at capacity |
| `lucent_cognitive_cycle_duration_seconds` | > 300s | Slow or stuck cognitive cycle |
| `http_requests_total{status=~"5.."}` | > 1% error rate | Server errors |
| `container_memory_working_set_bytes` | > 80% of limit | Memory pressure |
| `kube_pod_container_status_restarts_total` | > 3 in 10m | Crash-looping pods |

---

## Upgrading

### Helm Upgrade Procedure

1. **Review the changelog** for breaking changes in the new version.

2. **Back up the database:**

   ```bash
   DB_POD=$(kubectl -n lucent get pod -l app.kubernetes.io/component=db -o jsonpath='{.items[0].metadata.name}')
   kubectl -n lucent exec $DB_POD -- pg_dump -U lucent lucent | gzip > lucent-pre-upgrade.sql.gz
   ```

3. **Update chart dependencies** (if chart version changed):

   ```bash
   helm dependency update deploy/helm/lucent
   ```

4. **Preview the diff:**

   ```bash
   helm diff upgrade lucent deploy/helm/lucent \
     --namespace lucent \
     -f deploy/helm/lucent/values-prod.yaml
   ```

5. **Apply the upgrade:**

   ```bash
   helm upgrade lucent deploy/helm/lucent \
     --namespace lucent \
     -f deploy/helm/lucent/values-prod.yaml \
     --atomic \
     --timeout 10m
   ```

   The `--atomic` flag automatically rolls back if the upgrade fails.

6. **Verify rollout:**

   ```bash
   kubectl -n lucent rollout status deployment/lucent-server
   kubectl -n lucent get pods
   curl http://lucent.example.com/api/health
   ```

### Database Migration Handling

Lucent runs migrations on startup via the server's init sequence. The rolling update strategy
ensures:

1. New pod starts, runs migrations, passes readiness probe
2. Traffic shifts to the new pod
3. Old pod terminates

This means **migrations must be backward compatible** — the old code must continue to work
against the migrated schema until all pods are replaced.

For major migrations with breaking schema changes, use a maintenance window:

```bash
# Scale down to zero
kubectl -n lucent scale deployment/lucent-server --replicas=0

# Run migration job with new image
kubectl -n lucent run lucent-migrate --rm -i --restart=Never \
  --image=lucent:NEW_VERSION \
  --env="DATABASE_URL=..." \
  -- python -m lucent.migrate

# Scale back up with new image
helm upgrade lucent deploy/helm/lucent \
  --namespace lucent \
  --set server.image.tag=NEW_VERSION \
  --reuse-values
```

### Rollback

Helm keeps a revision history. Roll back to the previous revision if issues arise:

```bash
# List revision history
helm history lucent --namespace lucent

# Roll back to previous revision
helm rollback lucent --namespace lucent

# Roll back to a specific revision
helm rollback lucent 3 --namespace lucent
```

> **Note:** Helm rollback reverts the Kubernetes resources to a previous state but does **not**
> roll back the database schema. If the new migration was destructive, restore from the backup
> taken before the upgrade.

---

## Troubleshooting

### Pods stuck in `Pending`

**Symptom:** `kubectl -n lucent get pods` shows pods in `Pending` state.

**Diagnosis:**

```bash
kubectl -n lucent describe pod <pod-name>
# Look for: "Insufficient cpu", "Insufficient memory", "no nodes available"
```

**Solutions:**

- Scale up the node pool or add more nodes.
- Reduce resource requests: `--set server.resources.requests.cpu=100m`
- Check node taints: `kubectl get nodes -o json | jq '.items[].spec.taints'`

---

### CrashLoopBackOff — server pod restarts

**Diagnosis:**

```bash
kubectl -n lucent logs deployment/lucent-server --previous
```

**Common causes:**

| Log message | Cause | Fix |
|---|---|---|
| `DATABASE_URL not set` | Missing database secret | Verify `database.external.secretRef` is set and the secret exists |
| `LUCENT_SIGNING_SECRET not set` | Missing signing secret | Set `server.env.LUCENT_SIGNING_SECRET` |
| `Failed to connect to database` | DB unreachable | Check network policy, DB host, credentials |
| `Vault unreachable` | OpenBao not ready | Check OpenBao init container logs; may need manual unseal |

---

### Ingress returns 502/503

**Diagnosis:**

```bash
# Check ingress controller logs
kubectl -n ingress-nginx logs deployment/ingress-nginx-controller | tail -50

# Check service endpoints
kubectl -n lucent get endpoints lucent-server
```

**Solutions:**

- Ensure server pods are `Running` and passing readiness checks.
- Verify `ingress.className` matches your controller (`nginx`, `traefik`, etc.).
- Check that `server.service.port` matches the ingress backend port.

---

### `helm install` fails — dependency not found

**Symptom:** `Error: found in Chart.yaml, but missing in charts/ directory: postgresql`

**Fix:**

```bash
helm dependency update deploy/helm/lucent
helm install lucent deploy/helm/lucent --namespace lucent ...
```

---

### NetworkPolicy blocking traffic

When `networkPolicy.enabled=true`, only explicitly allowed traffic is permitted.

**Diagnosis:**

```bash
# Test connectivity from server to DB
kubectl -n lucent exec deployment/lucent-server -- nc -zv lucent-db 5432

# Temporarily disable to confirm network policy is the issue
helm upgrade lucent deploy/helm/lucent --namespace lucent \
  --reuse-values --set networkPolicy.enabled=false
```

---

### HPA not scaling

**Diagnosis:**

```bash
kubectl -n lucent describe hpa lucent-server
# Check: "unable to fetch metrics" or "metrics not available"
```

**Solutions:**

- Ensure `metrics-server` is installed: `kubectl top pods -n lucent`
- Verify resource requests are set — HPA calculates utilization as `usage / request`.
- Install metrics-server if missing: `helm install metrics-server metrics-server/metrics-server -n kube-system`

---

### OpenBao sealed after restart

The embedded OpenBao StatefulSet may return to a sealed state after pod restarts.

```bash
# Check seal status
OPENBAO_POD=$(kubectl -n lucent get pod -l app.kubernetes.io/component=openbao -o jsonpath='{.items[0].metadata.name}')
kubectl -n lucent exec $OPENBAO_POD -- bao status

# Unseal manually (you need the unseal key from initial setup)
kubectl -n lucent exec $OPENBAO_POD -- bao operator unseal <UNSEAL_KEY>
```

For production, configure auto-unseal using AWS KMS, Azure Key Vault, or the OpenBao transit
auto-unseal feature. See [operator-guide.md](operator-guide.md) for operator-managed unsealing.

---

### Checking daemon health

```bash
# View daemon logs
kubectl -n lucent logs deployment/lucent-server -c daemon --tail=100 -f

# Check daemon cognitive cycle via API (requires auth)
curl -H "Authorization: Bearer TOKEN" \
  http://lucent.example.com/api/daemon/status
```

---

For advanced deployment scenarios including declarative management, canary deployments, and
automated backup CronJobs, see [operator-guide.md](operator-guide.md).
