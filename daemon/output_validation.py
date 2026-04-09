"""Structured output extraction and validation for daemon task results."""

import json
import re
from typing import Any

from jsonschema import SchemaError, ValidationError, validate
from jsonschema.validators import validator_for

_OUTPUT_PATTERN = re.compile(r"<task_output>\s*([\s\S]*?)\s*</task_output>", re.DOTALL)
_VALID_ON_FAILURE = {"fail", "fallback", "retry_then_fallback"}


def validate_contract_schema(output_contract: dict | None) -> list[str]:
    """Validate output contract structure and embedded JSON Schema."""
    if output_contract is None:
        return []
    if not isinstance(output_contract, dict):
        return ["output_contract must be a JSON object"]

    errors: list[str] = []
    json_schema = output_contract.get("json_schema")
    if json_schema is None:
        return ["output_contract must contain a 'json_schema' key"]
    if not isinstance(json_schema, dict):
        return ["json_schema must be a JSON object"]

    try:
        cls = validator_for(json_schema)
        cls.check_schema(json_schema)
    except SchemaError as exc:
        errors.append(f"Invalid JSON Schema: {exc.message}")

    on_failure = output_contract.get("on_failure", "fallback")
    if on_failure not in _VALID_ON_FAILURE:
        errors.append(
            "output_contract.on_failure must be one of: "
            "fail, fallback, retry_then_fallback"
        )
    max_retries = output_contract.get("max_retries", 1)
    if not isinstance(max_retries, int) or max_retries < 0:
        errors.append("output_contract.max_retries must be an integer >= 0")
    return errors


def process_task_output(result_text: str | None, output_contract: dict | None) -> dict[str, Any]:
    """Extract and validate structured output from agent result text.

    Returns a canonical payload for completion:
      {
        "result_structured": dict | None,
        "result_summary": str | None,
        "validation_status": str,
        "validation_errors": list[str] | None,
      }
    """
    if not output_contract:
        return {
            "result_structured": None,
            "result_summary": None,
            "validation_status": "not_applicable",
            "validation_errors": None,
        }

    contract_errors = validate_contract_schema(output_contract)
    if contract_errors:
        return {
            "result_structured": None,
            "result_summary": None,
            "validation_status": "invalid",
            "validation_errors": contract_errors,
        }

    raw = result_text or ""
    match = _OUTPUT_PATTERN.search(raw)
    if not match:
        return {
            "result_structured": None,
            "result_summary": None,
            "validation_status": "extraction_failed",
            "validation_errors": ["No <task_output>...</task_output> block found"],
        }

    payload = match.group(1).strip()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        return {
            "result_structured": None,
            "result_summary": None,
            "validation_status": "invalid",
            "validation_errors": [f"Invalid JSON in <task_output>: {exc.msg}"],
        }

    schema = output_contract.get("json_schema", {})
    try:
        validate(instance=parsed, schema=schema)
    except ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path) or "<root>"
        return {
            "result_structured": None,
            "result_summary": None,
            "validation_status": "invalid",
            "validation_errors": [f"{path}: {exc.message}"],
        }

    summary = parsed.get("summary") if isinstance(parsed, dict) else None
    if summary is not None:
        summary = str(summary)[:2000]

    return {
        "result_structured": parsed,
        "result_summary": summary,
        "validation_status": "valid",
        "validation_errors": None,
    }
