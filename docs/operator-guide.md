# Lucent Kubernetes Operator Guide

This guide covers installing and using the Lucent Kubernetes operator to manage `LucentInstance` custom resources declaratively.

---

## Table of Contents

1. [Overview](#overview)
2. [Installation](#installation)
3. [CRD Reference](#crd-reference)
4. [Creating a Lucent Instance](#creating-a-lucent-instance)
5. [Upgrading Lucent](#upgrading-lucent)
6. [Backup and Restore](#backup-and-restore)
7. [Monitoring the Operator](#monitoring-the-operator)
8. [Advanced Topics](#advanced-topics)
9. [Uninstalling](#uninstalling)

---

## Overview

The Lucent operator manages the full lifecycle of Lucent deployments through a `LucentInstance` custom resource. It watches for changes, reconciles the desired state, runs database migrations before upgrades, manages backup CronJobs, and emits Kubernetes events for observability.

### Operator vs. raw Helm

| Scenario | Recommendation |
|---|---|
| Single deployment, manually managed | Raw Helm chart (`deploy/helm/lucent/`) |
| Multiple environments or namespaces | **Operator** — single source of truth per namespace |
| GitOps with automatic reconciliation | **Operator** — CR in Git, operator enforces state |
| Canary deployments with auto-promotion | **Operator** — built-in canary support |
| Frequent version upgrades | **Operator** — migration jobs run automatically |

The raw Helm chart and the operator are complementary. The operator internally generates Helm values and applies them; you can still tune behavior via `spec.helm.valuesOverrides`.

---

## Installation

### Prerequisites

- Kubernetes 1.25+
- `kubectl` configured for your cluster
- Cluster-admin privileges (for CRD and ClusterRole creation)

### 1. Apply CRDs

```bash
kubectl apply -f deploy/operator/crds/lucentinstance-crd.yaml
```

Verify the CRD is registered:

```bash
kubectl get crd lucentinstances.lucent.io
```

Expected output:

```
NAME                        CREATED AT
lucentinstances.lucent.io   2024-01-15T10:00:00Z
```

### 2. Deploy the Operator

Create the operator namespace and apply all manifests:

```bash
kubectl create namespace lucent-system

kubectl apply -f deploy/operator/manifests/serviceaccount.yaml
kubectl apply -f deploy/operator/manifests/clusterrole.yaml
kubectl apply -f deploy/operator/manifests/clusterrolebinding.yaml
kubectl apply -f deploy/operator/manifests/role.yaml
kubectl apply -f deploy/operator/manifests/rolebinding.yaml
kubectl apply -f deploy/operator/manifests/deployment.yaml
```

Or apply the entire directory at once:

```bash
kubectl apply -f deploy/operator/manifests/
```

### 3. Verify the Operator is Running

```bash
kubectl -n lucent-system get pods -l app.kubernetes.io/name=lucent-operator
```

```
NAME                               READY   STATUS    RESTARTS   AGE
lucent-operator-7d8b9f6c4d-xk9p2   1/1     Running   0          45s
```

Check the operator logs to confirm it has started and is watching for resources:

```bash
kubectl -n lucent-system logs -l app.kubernetes.io/name=lucent-operator --tail=30
```

Look for lines like:

```
INFO  kopf._core.engines.peering  Peering is set up, I am lucent-operator-...
INFO  kopf._cogs.engines.daemons  Starting the background watch for lucent.io/v1alpha1/lucentinstances
```

---

## CRD Reference

All fields live under `spec` of a `LucentInstance` resource (`apiVersion: lucent.io/v1alpha1`).

### Top-level fields

| Field | Type | Default | Description |
|---|---|---|---|
| `replicas` | integer | — (**required**) | Number of `lucent-server` pod replicas. Minimum: 1. |
| `image.repository` | string | `lucent` | Container image repository. |
| `image.tag` | string | `0.2.0` | Image tag / version to deploy. |
| `image.pullPolicy` | string | `IfNotPresent` | One of `Always`, `IfNotPresent`, `Never`. |

### `database`

Controls the PostgreSQL backend.

| Field | Type | Default | Description |
|---|---|---|---|
| `database.embedded` | boolean | `true` | `true` = deploy a PostgreSQL StatefulSet in the same namespace. `false` = connect to an external database. |
| `database.host` | string | — | External DB hostname. Required when `embedded: false`. |
| `database.port` | integer | `5432` | External DB port. |
| `database.secretRef` | string | — | Name of a `Secret` containing `DATABASE_URL` (or `PGUSER`, `PGPASSWORD`, `PGDATABASE`). Required when `embedded: false`. |
| `database.pvcName` | string | — | PVC name for embedded PostgreSQL data. Defaults to `<instance>-db-data`. |

### `vault`

Controls the OpenBao (Vault-compatible) secrets backend.

| Field | Type | Default | Description |
|---|---|---|---|
| `vault.embedded` | boolean | `true` | `true` = deploy an OpenBao StatefulSet in the same namespace. |
| `vault.address` | string | — | External Vault/OpenBao API URL (e.g. `http://vault:8200`). Required when `embedded: false`. |
| `vault.secretRef` | string | — | Name of a `Secret` containing `VAULT_TOKEN`. Required when `embedded: false`. |
| `vault.init` | boolean | `true` | Whether the operator should run the OpenBao init/unseal job on first deploy. |

### `models`

An array of LLM model configurations. Each entry has:

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Model identifier (e.g. `claude-opus-4.6`, `qwen2.5-coder-3b`). |
| `provider` | string | yes | One of `anthropic`, `openai`, `google`, `ollama`, `copilot`. |
| `engine` | string | yes | One of `copilot`, `langchain`. Controls which LLM backend processes requests. |
| `temperature` | number | no | Sampling temperature (provider-specific range). |
| `maxTokens` | integer | no | Maximum output token limit. Minimum: 1. |

The first model in the list determines the `LUCENT_LLM_ENGINE` setting applied to all pods.

### `daemon`

| Field | Type | Default | Description |
|---|---|---|---|
| `daemon.defaultModel` | string | `claude-opus-4.6` | Model ID used for daemon cognitive cycles. Must match an `id` in `models`. |
| `daemon.maxConcurrentSessions` | integer | `3` | Maximum number of parallel sub-agent sessions. Range: 1–100. |
| `daemon.intervalMinutes` | integer | `15` | Minutes between daemon cognitive cycles. Minimum: 1. |

### `observability`

| Field | Type | Default | Description |
|---|---|---|---|
| `observability.enabled` | boolean | `false` | Enable OpenTelemetry tracing and metrics export. |
| `observability.prometheusMonitor` | boolean | `false` | Create a `ServiceMonitor` for Prometheus Operator scraping. |
| `observability.otelEndpoint` | string | — | OTLP gRPC endpoint (e.g. `http://otel-collector:4317`). Required when `enabled: true`. |

### `canary`

| Field | Type | Default | Description |
|---|---|---|---|
| `canary.enabled` | boolean | `false` | Deploy a canary Deployment alongside the stable release. |
| `canary.imageTag` | string | — | Image tag for the canary pods. Required when `enabled: true`. |
| `canary.replicas` | integer | `1` | Number of canary pod replicas. |
| `canary.weight` | integer | `10` | Percentage of traffic routed to canary (0–100). Requires an ingress controller with traffic-splitting support. |
| `canary.promotionPolicy` | string | `manual` | `manual` = operator waits for human action. `auto` = operator promotes canary when health checks pass. |

### `backup`

| Field | Type | Default | Description |
|---|---|---|---|
| `backup.enabled` | boolean | `true` | Create and manage a `CronJob` for PostgreSQL dumps. |
| `backup.schedule` | string | `0 2 * * *` | Cron expression for the backup schedule (UTC). |
| `backup.retentionDays` | integer | `7` | Number of days to retain local backup files. Minimum: 1. |
| `backup.storage.pvcName` | string | — | PVC to write backup files to. Required when `backup.enabled: true`. |
| `backup.storage.s3Bucket` | string | — | S3 bucket name for off-site backup upload. Optional. |
| `backup.storage.s3Prefix` | string | — | Key prefix within the S3 bucket (e.g. `db/`). |
| `backup.storage.s3SecretRef` | string | — | Name of a `Secret` containing `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. Required when `s3Bucket` is set. |

### `helm`

Advanced overrides passed directly to the Helm chart.

| Field | Type | Default | Description |
|---|---|---|---|
| `helm.releaseName` | string | `<instance-name>` | Helm release name. |
| `helm.chartPath` | string | `deploy/helm/lucent` | Path to the Helm chart directory. |
| `helm.namespace` | string | CR namespace | Target namespace for the Helm release. |
| `helm.valuesOverrides` | object | — | Arbitrary `values.yaml` overrides merged on top of operator-generated values. Use for settings the operator does not expose directly. |

### Status fields

The operator writes the following fields to `.status`:

| Field | Description |
|---|---|
| `phase` | Current lifecycle phase: `Pending`, `Initializing`, `Running`, `Degraded`, `Failed`, `Terminating`. |
| `observedGeneration` | The `.metadata.generation` this status reflects. |
| `version` | The image tag currently running. |
| `activeCanary` | Image tag of the active canary Deployment, if any. |
| `readyReplicas` | Number of ready stable replicas. |
| `lastReconcileTime` | ISO-8601 timestamp of the last successful reconciliation. |
| `health.server` | `Healthy` / `Unhealthy` / `Unknown` |
| `health.database` | `Healthy` / `Unhealthy` / `Unknown` |
| `health.vault` | `Healthy` / `Unhealthy` / `Unknown` |
| `conditions` | Standard Kubernetes condition array (`Ready`, `DatabaseReady`, `VaultReady`). |

---

## Creating a Lucent Instance

### 1. Create a namespace

```bash
kubectl create namespace lucent
```

### 2. Create credential secrets (if using external services)

```bash
# External PostgreSQL
kubectl -n lucent create secret generic lucent-db-credentials \
  --from-literal=DATABASE_URL="postgresql://lucent:password@pg-host:5432/lucent"

# API keys for LLM providers (langchain engine)
kubectl -n lucent create secret generic lucent-api-keys \
  --from-literal=ANTHROPIC_API_KEY="sk-ant-..."
```

### 3. Apply the CR

Save the following as `my-instance.yaml` and apply it:

```yaml
apiVersion: lucent.io/v1alpha1
kind: LucentInstance
metadata:
  name: production
  namespace: lucent
spec:
  replicas: 2
  image:
    repository: ghcr.io/lucent/lucent
    tag: "0.2.0"
  database:
    embedded: false
    host: pg-cluster.default.svc
    secretRef: lucent-db-credentials
  vault:
    embedded: true
    init: true
  models:
    - id: claude-opus-4.6
      provider: anthropic
      engine: copilot
  daemon:
    defaultModel: claude-opus-4.6
    maxConcurrentSessions: 5
  observability:
    enabled: true
    prometheusMonitor: true
    otelEndpoint: http://otel-collector:4317
  backup:
    enabled: true
    schedule: "0 1 * * *"
    retentionDays: 14
    storage:
      pvcName: lucent-db-backups
```

```bash
kubectl apply -f my-instance.yaml
```

### 4. Watch reconciliation progress

```bash
kubectl -n lucent get lucentinstance production -w
```

```
NAME         READY       REPLICAS   VERSION   AGE
production   Pending     2          0.2.0     5s
production   Running     2          0.2.0     42s
```

### 5. Verify pods are running

```bash
kubectl -n lucent get pods
```

```
NAME                              READY   STATUS    RESTARTS   AGE
production-server-6f9b4d-xk2p1    1/1     Running   0          60s
production-server-6f9b4d-lm3q9    1/1     Running   0          60s
production-openbao-0              1/1     Running   0          55s
```

---

## Upgrading Lucent

### How the operator handles upgrades

When you update `spec.image.tag` in the CR, the operator follows this sequence:

1. **Migration job** — A `Job` running the new image with `migrate` command is created and must complete successfully before pods are updated.
2. **Rolling update** — The stable `Deployment` is patched with the new image tag. Kubernetes performs a rolling update respecting `maxUnavailable`/`maxSurge` settings.
3. **Status update** — `status.version` is updated to the new tag once all replicas are ready.

If the migration job fails, the operator emits a `Warning` event and stops the rollout. Existing pods continue running the previous version.

### Performing an in-place upgrade

```bash
kubectl -n lucent patch lucentinstance production \
  --type=merge \
  -p '{"spec":{"image":{"tag":"0.3.0"}}}'
```

Watch migration job completion:

```bash
kubectl -n lucent get jobs -w
```

```
NAME                         COMPLETIONS   DURATION   AGE
production-migrate-0.3.0     0/1           5s         5s
production-migrate-0.3.0     1/1           18s        18s
```

### Canary deployments

See [Canary Deployments](#canary-deployments) in Advanced Topics.

---

## Backup and Restore

### CronJob configuration

When `spec.backup.enabled` is `true`, the operator creates and reconciles a `CronJob` in the same namespace. The job:

- Runs `pg_dump -Fc` against the database
- Writes the dump file to the configured PVC under a timestamped filename
- Optionally uploads the dump to S3 using the `aws s3 cp` command

Example CR snippet:

```yaml
spec:
  backup:
    enabled: true
    schedule: "0 1 * * *"      # 1:00 AM UTC daily
    retentionDays: 14
    storage:
      pvcName: lucent-db-backups
      s3Bucket: my-backups-bucket
      s3Prefix: lucent/prod/
      s3SecretRef: lucent-backup-s3
```

Create the S3 credentials secret:

```bash
kubectl -n lucent create secret generic lucent-backup-s3 \
  --from-literal=AWS_ACCESS_KEY_ID="AKIA..." \
  --from-literal=AWS_SECRET_ACCESS_KEY="..."
```

### Manual backup

Trigger a backup immediately by creating a one-off `Job` from the `CronJob`:

```bash
kubectl -n lucent create job --from=cronjob/lucent-db-backup manual-backup-$(date +%s)
```

Follow the job logs:

```bash
kubectl -n lucent logs -l job-name=manual-backup-... -f
```

### Restore procedure

1. **Scale down the server** to prevent writes during restore:

   ```bash
   kubectl -n lucent patch lucentinstance production \
     --type=merge -p '{"spec":{"replicas":0}}'
   ```

2. **Identify the backup file** on the PVC:

   ```bash
   kubectl -n lucent run restore-shell --rm -it \
     --image=postgres:16-alpine \
     --overrides='{"spec":{"volumes":[{"name":"bk","persistentVolumeClaim":{"claimName":"lucent-db-backups"}}],"containers":[{"name":"c","image":"postgres:16-alpine","command":["ls","-lh","/backup"],"volumeMounts":[{"name":"bk","mountPath":"/backup"}]}]}}' \
     -- /bin/sh
   ```

3. **Run the restore** using the chosen backup file:

   ```bash
   kubectl -n lucent run restore --rm -it \
     --image=postgres:16-alpine \
     --env="DATABASE_URL=postgresql://lucent:password@pg-host:5432/lucent" \
     --overrides='{"spec":{"volumes":[{"name":"bk","persistentVolumeClaim":{"claimName":"lucent-db-backups"}}],"containers":[{"name":"c","image":"postgres:16-alpine","command":["pg_restore","--clean","--if-exists","-d","$(DATABASE_URL)","/backup/20240115T010000Z.sql"],"volumeMounts":[{"name":"bk","mountPath":"/backup"}]}]}}' \
     -- /bin/sh
   ```

4. **Scale the server back up**:

   ```bash
   kubectl -n lucent patch lucentinstance production \
     --type=merge -p '{"spec":{"replicas":2}}'
   ```

---

## Monitoring the Operator

### Kubernetes events

The operator emits events on the `LucentInstance` object. View them with:

```bash
kubectl -n lucent describe lucentinstance production
```

Look for the `Events:` section at the bottom. Common events:

| Reason | Type | Meaning |
|---|---|---|
| `ReconcileStarted` | Normal | Operator began processing a change. |
| `ReconcileSucceeded` | Normal | Reconciliation completed without errors. |
| `MigrationStarted` | Normal | Database migration job created. |
| `MigrationSucceeded` | Normal | Migration job completed successfully. |
| `MigrationFailed` | Warning | Migration job failed — rollout halted. |
| `CanaryDeployed` | Normal | Canary Deployment created. |
| `HealthCheckFailed` | Warning | `/api/health` returned a non-200 response. |
| `ReconcileError` | Warning | Unexpected error during reconciliation (see message). |

### Operator logs

Stream operator logs in real time:

```bash
kubectl -n lucent-system logs -l app.kubernetes.io/name=lucent-operator -f
```

For structured JSON output (useful for log aggregation), set `LUCENT_LOG_FORMAT=json` in the operator Deployment env.

Filter for warnings and errors only:

```bash
kubectl -n lucent-system logs -l app.kubernetes.io/name=lucent-operator | grep -E 'WARNING|ERROR'
```

### Health indicators

Check the operator pod liveness via its health port:

```bash
kubectl -n lucent-system port-forward deploy/lucent-operator 8080:8080 &
curl -s http://localhost:8080/healthz
```

Key status fields to watch on the CR:

```bash
kubectl -n lucent get lucentinstance production \
  -o jsonpath='{.status}' | jq .
```

```json
{
  "phase": "Running",
  "version": "0.2.0",
  "readyReplicas": 2,
  "lastReconcileTime": "2024-01-15T02:05:33Z",
  "health": {
    "server": "Healthy",
    "database": "Healthy",
    "vault": "Healthy"
  },
  "conditions": [
    {
      "type": "Ready",
      "status": "True",
      "reason": "ReconcileSucceeded",
      "lastTransitionTime": "2024-01-15T02:05:33Z"
    }
  ]
}
```

---

## Advanced Topics

### Canary Deployments

A canary deployment runs a new image version alongside the stable release, receiving a configurable percentage of traffic. This lets you validate the new version before a full rollout.

**Enable a canary:**

```yaml
spec:
  image:
    tag: "0.2.0"          # stable
  canary:
    enabled: true
    imageTag: "0.3.0-rc1" # canary
    replicas: 1
    weight: 10             # 10% of traffic
    promotionPolicy: manual
```

The operator creates a second `Deployment` (`<name>-canary`) and — if your ingress controller supports it — configures traffic splitting annotations automatically.

**Promote the canary manually:**

Once you've validated the canary, promote it by updating the stable image tag and disabling the canary:

```bash
kubectl -n lucent patch lucentinstance production --type=merge -p \
  '{"spec":{"image":{"tag":"0.3.0-rc1"},"canary":{"enabled":false}}}'
```

The operator runs the migration job, promotes the stable Deployment to the new image, and removes the canary Deployment.

**Auto-promotion** (`promotionPolicy: auto`): the operator monitors the canary pods' `/api/health` endpoint. After a configurable settling period with no health check failures, it automatically patches the stable image tag and removes the canary.

### Multi-Instance (Multiple Namespaces)

The operator watches all namespaces by default (`--all-namespaces`). You can deploy independent `LucentInstance` resources in separate namespaces with completely isolated databases and configurations:

```bash
kubectl create namespace lucent-staging
kubectl create namespace lucent-prod

kubectl apply -f staging-instance.yaml -n lucent-staging
kubectl apply -f prod-instance.yaml    -n lucent-prod
```

Each instance gets its own reconciliation loop, backup CronJob, and status. The operator uses namespaced RBAC (`Role`/`RoleBinding`) in addition to cluster-scoped permissions, so resource creation is namespace-isolated.

List all instances across all namespaces:

```bash
kubectl get lucentinstance --all-namespaces
```

```
NAMESPACE        NAME         READY     REPLICAS   VERSION   AGE
lucent-staging   staging      Running   1          0.2.0     2d
lucent-prod      production   Running   3          0.2.0     7d
```

### Custom Resource Status Interpretation

| `status.phase` | Meaning | Action |
|---|---|---|
| `Pending` | CR created, operator hasn't processed it yet. | Wait a few seconds; check operator logs if it stays here. |
| `Initializing` | First-time setup: running init/unseal jobs. | Wait for jobs to complete. |
| `Running` | All replicas ready and healthy. | No action needed. |
| `Degraded` | Some replicas ready, health checks partially failing. | Check `status.health` and `status.conditions`. |
| `Failed` | Unrecoverable error (e.g., migration failure). | Check events and operator logs; manual intervention required. |
| `Terminating` | CR deletion in progress. | Finalizer is running; do not interrupt. |

A `Degraded` instance may auto-recover if the underlying cause is transient (e.g., a restarting pod). A `Failed` instance requires manual investigation before the operator will retry.

### Leader Election and HA Operator

For high availability, run multiple operator replicas. The operator uses kopf's built-in leader election via a `KopfPeering` custom resource.

Scale the operator Deployment:

```bash
kubectl -n lucent-system scale deployment lucent-operator --replicas=2
```

Only one replica actively reconciles at a time. If the leader pod is terminated, a standby replica takes over within the kopf peering timeout (default: 30s).

The peering object can be inspected:

```bash
kubectl -n lucent-system get kopfpeeringsnamespaced.kopf.dev
```

> **Note:** The `KopfPeering` CRD is installed automatically when kopf starts. If you see `no matches for kind "KopfPeering"` in logs, apply the kopf CRD from the [kopf releases](https://github.com/nolar/kopf/releases).

---

## Uninstalling

Follow this order to avoid orphaned resources.

### 1. Delete all LucentInstance CRs

```bash
kubectl delete lucentinstance --all --all-namespaces
```

Wait until all instances are fully deleted (finalizers must complete):

```bash
kubectl get lucentinstance --all-namespaces
# Should return: No resources found.
```

If an instance is stuck in `Terminating`, check for finalizer issues:

```bash
# Force-remove finalizers only if operator is confirmed stopped
kubectl -n lucent patch lucentinstance production \
  --type=json -p '[{"op":"remove","path":"/metadata/finalizers"}]'
```

### 2. Delete the operator

```bash
kubectl delete -f deploy/operator/manifests/
```

### 3. Delete the operator namespace

```bash
kubectl delete namespace lucent-system
```

### 4. Delete the CRDs

Deleting CRDs will cascade-delete any remaining CR objects at the API level:

```bash
kubectl delete -f deploy/operator/crds/lucentinstance-crd.yaml
```

### 5. Clean up instance namespaces

If you created dedicated namespaces for Lucent instances:

```bash
kubectl delete namespace lucent
kubectl delete namespace lucent-staging
# etc.
```

> **Warning:** Deleting a namespace is irreversible and will delete all resources within it, including PVCs. Ensure backups are complete before namespace deletion.
