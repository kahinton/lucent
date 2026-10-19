"""Tests for the task-kind registry and legacy inference."""

from __future__ import annotations

import pytest

from lucent.task_kinds import (
    UnknownTaskKindError,
    resolve_task_kind,
    task_kind_catalog,
    task_kind_names,
    task_kind_spec,
    validate_task_kind_payload,
)


def test_registered_kinds_are_agent_decision_fusion():
    assert task_kind_names() == ["agent", "decision", "fusion"]
    assert task_kind_spec("decision-step").kind == "decision"


def test_resolve_task_kind_prefers_explicit_kind_and_infers_legacy():
    assert resolve_task_kind(kind="agent", decision_config={"questions": []}) == "agent"
    assert resolve_task_kind(decision_config={"questions": []}) == "decision"
    assert resolve_task_kind(agent_type="decision-step") == "decision"
    assert resolve_task_kind(fusion_config={"mode": "best_of_n"}) == "fusion"
    assert resolve_task_kind(agent_type="code") == "agent"
    with pytest.raises(UnknownTaskKindError):
        resolve_task_kind(kind="run_code")


def test_catalog_exposes_schemas_examples_and_requirements():
    catalog = {entry["kind"]: entry for entry in task_kind_catalog()}
    decision = catalog["decision"]
    assert decision["config_field"] == "decision_config"
    assert decision["config_required"] is True
    assert decision["config_schema"]["required"] == ["questions"]
    assert decision["examples"]
    assert decision["requirements"]["model_class"] == "decision"
    assert decision["requirements"]["agent_type_const"] == "decision-step"
    assert decision["requirements"]["backend_allowed"] is False
    assert decision["requirements"]["output_contract_allowed"] is False
    assert catalog["agent"]["requirements"]["sandbox"] == "optional"
    assert catalog["fusion"]["requirements"]["sandbox"] == "forbidden"


def test_validate_task_kind_payload():
    validate_task_kind_payload("agent", None)
    with pytest.raises(ValueError, match="requires"):
        validate_task_kind_payload("decision", None)
    with pytest.raises(ValueError, match="does not accept execution_config"):
        validate_task_kind_payload("agent", {"anything": True})
    with pytest.raises(ValueError):
        validate_task_kind_payload(
            "decision", {"questions": [{"name": "q", "type": "bogus", "instructions": "x"}]}
        )
