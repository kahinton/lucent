from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

import kopf
from kubernetes import client, config
from kubernetes.client.rest import ApiException

GROUP = "lucent.io"
VERSION = "v1alpha1"
PLURAL = "lucentinstances"
OPERATOR_LABEL = "app.kubernetes.io/name=lucent-operator"

logger = logging.getLogger(__name__)


@kopf.on.startup()
def on_startup(settings: kopf.OperatorSettings, **_: Any) -> None:
    settings.posting.level = logging.INFO
    settings.peering.name = "lucent-operator-peering"
    settings.peering.standalone = False
    settings.networking.error_backoffs = [5, 10, 20, 40]
    settings.watching.connect_timeout = 30
    settings.watching.server_timeout = 60


@kopf.on.login()
def login(**_: Any) -> kopf.ConnectionInfo:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return kopf.login_via_client()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for key, value in b.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _build_helm_values(name: str, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
    image = spec.get("image", {})
    database = spec.get("database", {})
    vault = spec.get("vault", {})
    daemon = spec.get("daemon", {})
    observability = spec.get("observability", {})
    models = spec.get("models", [])
    canary = spec.get("canary", {})

    values: dict[str, Any] = {
        "server": {
            "replicaCount": spec.get("replicas", 1),
            "image": {
                "repository": image.get("repository", "lucent"),
                "tag": image.get("tag", "0.2.0"),
                "pullPolicy": image.get("pullPolicy", "IfNotPresent"),
            },
        },
        "database": {
            "embedded": database.get("embedded", True),
            "external": {
                "host": database.get("host", ""),
                "port": database.get("port", 5432),
                "secretRef": database.get("secretRef", ""),
            },
        },
        "openbao": {
            "embedded": vault.get("embedded", True),
            "external": {
                "address": vault.get("address", ""),
                "secretRef": vault.get("secretRef", ""),
            },
        },
        "llm": {
            "engine": models[0].get("engine", "copilot") if models else "copilot",
            "models": models,
        },
        "daemon": {
            "enabled": True,
            "model": daemon.get("defaultModel", "claude-opus-4.6"),
            "maxConcurrentSessions": daemon.get("maxConcurrentSessions", 3),
            "env": {
                "LUCENT_DAEMON_INTERVAL": str(daemon.get("intervalMinutes", 15)),
            },
        },
        "observability": {
            "enabled": observability.get("enabled", False),
            "prometheus": {
                "serviceMonitor": observability.get("prometheusMonitor", False),
            },
        },
        "operator": {
            "managedBy": "lucent-operator",
            "instance": name,
            "namespace": namespace,
            "canary": canary,
        },
    }

    overrides = spec.get("helm", {}).get("valuesOverrides", {})
    if isinstance(overrides, dict) and overrides:
        values = _merge(values, overrides)

    return values


def _upsert_config_map(namespace: str, name: str, data: dict[str, str]) -> None:
    api = client.CoreV1Api()
    body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels={
                "app.kubernetes.io/name": "lucent",
                "app.kubernetes.io/component": "operator-values",
            },
        ),
        data=data,
    )
    try:
        api.read_namespaced_config_map(name=name, namespace=namespace)
        api.replace_namespaced_config_map(name=name, namespace=namespace, body=body)
    except ApiException as exc:
        if exc.status == 404:
            api.create_namespaced_config_map(namespace=namespace, body=body)
        else:
            raise


def _upsert_cronjob(namespace: str, name: str, schedule: str, spec: dict[str, Any]) -> None:
    batch = client.BatchV1Api()
    backup = spec.get("backup", {})
    storage = backup.get("storage", {})

    env = [
        client.V1EnvVar(name="PGHOST", value=spec.get("database", {}).get("host", "")),
        client.V1EnvVar(name="PGPORT", value=str(spec.get("database", {}).get("port", 5432))),
        client.V1EnvVar(name="S3_BUCKET", value=storage.get("s3Bucket", "")),
        client.V1EnvVar(name="S3_PREFIX", value=storage.get("s3Prefix", "")),
    ]

    command = [
        "/bin/sh",
        "-ec",
        (
            "TS=$(date -u +%Y%m%dT%H%M%SZ); "
            "OUT=/backup/${TS}.sql; "
            "pg_dump -Fc \"$DATABASE_URL\" > ${OUT}; "
            "if [ -n \"$S3_BUCKET\" ]; then "
            "  aws s3 cp ${OUT} s3://${S3_BUCKET}/${S3_PREFIX}${TS}.sql; "
            "fi"
        ),
    ]

    env_from = [
        client.V1EnvFromSource(
            secret_ref=client.V1SecretEnvSource(
                name=spec.get("database", {}).get("secretRef", "lucent-db-credentials")
            )
        )
    ]
    s3_secret_ref = storage.get("s3SecretRef", "")
    if s3_secret_ref:
        env_from.append(
            client.V1EnvFromSource(secret_ref=client.V1SecretEnvSource(name=s3_secret_ref))
        )

    pod_spec = client.V1PodSpec(
        restart_policy="OnFailure",
        service_account_name="lucent-operator",
        security_context=client.V1PodSecurityContext(
            run_as_non_root=True,
            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
            fs_group=999,
        ),
        containers=[
            client.V1Container(
                name="backup",
                image="postgres:16-alpine",
                image_pull_policy="IfNotPresent",
                command=command,
                env=env,
                env_from=env_from,
                volume_mounts=[
                    client.V1VolumeMount(name="backup", mount_path="/backup"),
                ],
                security_context=client.V1SecurityContext(
                    allow_privilege_escalation=False,
                    read_only_root_filesystem=True,
                    capabilities=client.V1Capabilities(drop=["ALL"]),
                    run_as_non_root=True,
                    run_as_user=999,
                    run_as_group=999,
                ),
            )
        ],
        volumes=[
            client.V1Volume(
                name="backup",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=storage.get("pvcName", spec.get("database", {}).get("pvcName", "lucent-db-data"))
                ),
            )
        ],
    )

    body = client.V1CronJob(
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels={
                "app.kubernetes.io/name": "lucent",
                "app.kubernetes.io/component": "backup",
            },
        ),
        spec=client.V1CronJobSpec(
            schedule=schedule,
            concurrency_policy="Forbid",
            successful_jobs_history_limit=3,
            failed_jobs_history_limit=3,
            job_template=client.V1JobTemplateSpec(
                spec=client.V1JobSpec(
                    template=client.V1PodTemplateSpec(
                        spec=pod_spec,
                    )
                )
            ),
        ),
    )

    try:
        batch.read_namespaced_cron_job(name=name, namespace=namespace)
        batch.replace_namespaced_cron_job(name=name, namespace=namespace, body=body)
    except ApiException as exc:
        if exc.status == 404:
            batch.create_namespaced_cron_job(namespace=namespace, body=body)
        else:
            raise


def _run_job(namespace: str, name: str, image: str, command: list[str], env: list[client.V1EnvVar] | None = None) -> None:
    batch = client.BatchV1Api()
    body = client.V1Job(
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels={
                "app.kubernetes.io/name": "lucent",
                "app.kubernetes.io/component": "operator-job",
            },
        ),
        spec=client.V1JobSpec(
            backoff_limit=1,
            ttl_seconds_after_finished=600,
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    restart_policy="Never",
                    service_account_name="lucent-operator",
                    security_context=client.V1PodSecurityContext(
                        run_as_non_root=True,
                        seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        client.V1Container(
                            name="job",
                            image=image,
                            image_pull_policy="IfNotPresent",
                            command=command,
                            env=env or [],
                            security_context=client.V1SecurityContext(
                                allow_privilege_escalation=False,
                                read_only_root_filesystem=True,
                                capabilities=client.V1Capabilities(drop=["ALL"]),
                                run_as_non_root=True,
                            ),
                        )
                    ],
                )
            ),
        ),
    )

    try:
        batch.create_namespaced_job(namespace=namespace, body=body)
    except ApiException as exc:
        if exc.status == 409:
            logger.info("job already exists: %s/%s", namespace, name)
            return
        raise


def _read_server_health(namespace: str, name: str) -> tuple[str, int]:
    apps = client.AppsV1Api()
    deployment_name = f"{name}-server"
    try:
        deployment = apps.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        ready = deployment.status.ready_replicas or 0
        desired = deployment.spec.replicas or 0
        return ("Healthy" if desired > 0 and ready >= desired else "Degraded", ready)
    except ApiException as exc:
        if exc.status == 404:
            return ("Unknown", 0)
        raise


def _check_openbao(namespace: str, name: str) -> str:
    apps = client.AppsV1Api()
    sts_name = f"{name}-openbao"
    try:
        sts = apps.read_namespaced_stateful_set(name=sts_name, namespace=namespace)
        ready = sts.status.ready_replicas or 0
        desired = sts.spec.replicas or 0
        return "Healthy" if desired == 0 or ready >= desired else "Degraded"
    except ApiException as exc:
        if exc.status == 404:
            return "External"
        raise


def _patch_status(namespace: str, name: str, patch: dict[str, Any]) -> None:
    api = client.CustomObjectsApi()
    api.patch_namespaced_custom_object_status(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=PLURAL,
        name=name,
        body={"status": patch},
    )


def _reconcile(name: str, namespace: str, spec: dict[str, Any], generation: int) -> dict[str, Any]:
    helm_values = _build_helm_values(name=name, namespace=namespace, spec=spec)
    values_name = f"{name}-helm-values"
    _upsert_config_map(namespace=namespace, name=values_name, data={"values.yaml": json.dumps(helm_values, indent=2)})

    image_repo = spec.get("image", {}).get("repository", "lucent")
    image_tag = spec.get("image", {}).get("tag", "0.2.0")
    image = f"{image_repo}:{image_tag}"

    _run_job(
        namespace=namespace,
        name=f"{name}-migration-{generation}",
        image=image,
        command=["python", "-m", "lucent.server"],
    )

    if spec.get("vault", {}).get("embedded", True) and spec.get("vault", {}).get("init", True):
        _run_job(
            namespace=namespace,
            name=f"{name}-openbao-init-{generation}",
            image="openbao/openbao:2.1.0",
            command=["/bin/sh", "-ec", "echo Initializing OpenBao; exit 0"],
        )

    canary = spec.get("canary", {})
    active_canary = ""
    if canary.get("enabled", False) and canary.get("imageTag"):
        canary_values = _build_helm_values(name=name, namespace=namespace, spec=spec)
        canary_values["server"]["image"]["tag"] = canary["imageTag"]
        canary_values["server"]["replicaCount"] = canary.get("replicas", 1)
        canary_cm = f"{name}-helm-values-canary"
        _upsert_config_map(
            namespace=namespace,
            name=canary_cm,
            data={"values.yaml": json.dumps(canary_values, indent=2)},
        )
        active_canary = canary_cm

    if spec.get("backup", {}).get("enabled", True):
        _upsert_cronjob(
            namespace=namespace,
            name=f"{name}-db-backup",
            schedule=spec.get("backup", {}).get("schedule", "0 2 * * *"),
            spec=spec,
        )

    server_health, ready_replicas = _read_server_health(namespace=namespace, name=name)
    vault_health = _check_openbao(namespace=namespace, name=name)
    db_health = "Embedded" if spec.get("database", {}).get("embedded", True) else "External"

    phase = "Ready" if server_health == "Healthy" else "Progressing"
    return {
        "phase": phase,
        "observedGeneration": generation,
        "version": image_tag,
        "activeCanary": active_canary,
        "readyReplicas": ready_replicas,
        "lastReconcileTime": _now(),
        "health": {
            "server": server_health,
            "database": db_health,
            "vault": vault_health,
            "details": {
                "helmValuesConfigMap": values_name,
            },
        },
        "conditions": [
            {
                "type": "Ready",
                "status": "True" if phase == "Ready" else "False",
                "reason": "Reconciled",
                "message": "Reconciliation completed",
                "observedGeneration": generation,
                "lastTransitionTime": _now(),
            }
        ],
    }


@kopf.on.create(GROUP, VERSION, PLURAL)
def on_create(
    spec: dict[str, Any], name: str, namespace: str, meta: dict[str, Any], body: dict[str, Any], **_: Any
) -> dict[str, Any]:
    generation = int(meta.get("generation", 1))
    status = _reconcile(name=name, namespace=namespace, spec=spec, generation=generation)
    _patch_status(namespace=namespace, name=name, patch=status)
    if status.get("phase") != "Ready":
        kopf.event(body, type="Warning", reason="ReconcileProgressing", message="Instance is still progressing")
    else:
        kopf.event(body, type="Normal", reason="Reconciled", message="Instance reconciled successfully")
    return status


@kopf.on.update(GROUP, VERSION, PLURAL)
def on_update(
    spec: dict[str, Any], name: str, namespace: str, meta: dict[str, Any], body: dict[str, Any], **_: Any
) -> dict[str, Any]:
    generation = int(meta.get("generation", 1))
    status = _reconcile(name=name, namespace=namespace, spec=spec, generation=generation)
    _patch_status(namespace=namespace, name=name, patch=status)
    if status.get("phase") != "Ready":
        kopf.event(body, type="Warning", reason="HealthDegraded", message="Instance requires attention")
    else:
        kopf.event(body, type="Normal", reason="Reconciled", message="Instance reconciled successfully")
    return status


@kopf.timer(GROUP, VERSION, PLURAL, interval=60.0, sharp=True)
def health_timer(
    spec: dict[str, Any], name: str, namespace: str, body: dict[str, Any], logger: logging.Logger, **_: Any
) -> None:
    server_health, ready_replicas = _read_server_health(namespace=namespace, name=name)
    vault_health = _check_openbao(namespace=namespace, name=name)
    db_health = "Embedded" if spec.get("database", {}).get("embedded", True) else "External"

    phase = "Ready" if server_health == "Healthy" else "Degraded"
    patch = {
        "phase": phase,
        "readyReplicas": ready_replicas,
        "lastReconcileTime": _now(),
        "health": {
            "server": server_health,
            "database": db_health,
            "vault": vault_health,
            "details": {"source": "timer"},
        },
    }
    _patch_status(namespace=namespace, name=name, patch=patch)

    if server_health != "Healthy":
        logger.warning("LucentInstance %s/%s unhealthy: server=%s", namespace, name, server_health)
        kopf.event(body, type="Warning", reason="HealthCheckFailed", message=f"Server state: {server_health}")


@kopf.on.delete(GROUP, VERSION, PLURAL)
def on_delete(name: str, namespace: str, **_: Any) -> None:
    batch = client.BatchV1Api()
    core = client.CoreV1Api()

    for job_name in [f"{name}-db-backup"]:
        try:
            batch.delete_namespaced_cron_job(name=job_name, namespace=namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise

    for cm_name in [f"{name}-helm-values", f"{name}-helm-values-canary"]:
        try:
            core.delete_namespaced_config_map(name=cm_name, namespace=namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
