from unittest.mock import MagicMock

import pytest

from lucent.sandbox.k8s_backend import KubernetesBackend, _k8s_memory
from lucent.sandbox.models import SandboxConfig


def test_k8s_memory_uses_binary_quantities():
    assert _k8s_memory("512m") == "512Mi"
    assert _k8s_memory("2g") == "2Gi"
    assert _k8s_memory("1Gi") == "1Gi"


def test_pod_manifest_uses_portable_template_fields_only():
    backend = KubernetesBackend(namespace="test-sandboxes")
    config = SandboxConfig(
        image="python:3.12-slim",
        env_vars={"CI": "true"},
        working_dir="/workspace",
        memory_limit="4g",
        cpu_limit=3.0,
        disk_limit="20g",
        timeout_seconds=3600,
        docker_bind_mounts=[
            {"source": "/host/cache", "target": "/cache", "read_only": False}
        ],
    )

    manifest = backend._pod_manifest("sandbox-id", "sandbox-name", config)
    pod_spec = manifest["spec"]
    container = pod_spec["containers"][0]

    assert container["image"] == "python:3.12-slim"
    assert container["env"] == [{"name": "CI", "value": "true"}]
    assert container["resources"]["limits"] == {"cpu": "3.0", "memory": "4Gi"}
    assert pod_spec["volumes"] == [
        {"name": "workspace", "emptyDir": {"sizeLimit": "20Gi"}}
    ]
    assert container["volumeMounts"] == [{"name": "workspace", "mountPath": "/workspace"}]
    assert pod_spec["activeDeadlineSeconds"] == 3600
    assert "/host/cache" not in str(manifest)


@pytest.mark.asyncio
async def test_isolated_network_mode_creates_deny_egress_policy():
    backend = KubernetesBackend(namespace="test-sandboxes")
    networking = MagicMock()
    backend._core = MagicMock()
    backend._networking = networking

    await backend._apply_network_policy(
        "sandbox-id", "sandbox-name", SandboxConfig(network_mode="none")
    )

    namespace, policy = networking.create_namespaced_network_policy.call_args.args
    assert namespace == "test-sandboxes"
    assert policy["spec"]["policyTypes"] == ["Egress"]
    assert policy["spec"]["egress"] == []
