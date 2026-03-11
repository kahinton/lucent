"""Validation utilities for memory metadata."""

from typing import Any, get_args, get_origin

from pydantic import ValidationError
from pydantic.fields import FieldInfo

from lucent.models.memory import (
    ExperienceMetadata,
    GoalMetadata,
    IndividualMetadata,
    MemoryType,
    ProceduralMetadata,
    TechnicalMetadata,
)

# Mapping of memory types to their metadata models
METADATA_MODELS = {
    MemoryType.EXPERIENCE: ExperienceMetadata,
    MemoryType.TECHNICAL: TechnicalMetadata,
    MemoryType.PROCEDURAL: ProceduralMetadata,
    MemoryType.GOAL: GoalMetadata,
    MemoryType.INDIVIDUAL: IndividualMetadata,
}

# Also support string keys for convenience
METADATA_MODELS_BY_STR = {
    "experience": ExperienceMetadata,
    "technical": TechnicalMetadata,
    "procedural": ProceduralMetadata,
    "goal": GoalMetadata,
    "individual": IndividualMetadata,
}


def validate_metadata(
    memory_type: str | MemoryType, metadata: dict[str, Any] | None
) -> dict[str, Any]:
    """Validate and normalize metadata for a given memory type.

    Args:
        memory_type: The type of memory (string or MemoryType enum).
        metadata: The metadata dict to validate. Can be None or empty.

    Returns:
        Validated and normalized metadata dict.

    Raises:
        ValueError: If the memory type is invalid or metadata doesn't match the schema.
    """
    # Handle empty/None metadata - return empty dict
    if not metadata:
        return {}

    # Normalize memory type to string
    if isinstance(memory_type, MemoryType):
        type_str = memory_type.value
    else:
        type_str = memory_type.lower()

    # Get the appropriate metadata model
    metadata_model = METADATA_MODELS_BY_STR.get(type_str)
    if metadata_model is None:
        raise ValueError(f"Invalid memory type: {memory_type}")

    # Validate metadata against the model
    try:
        validated = metadata_model.model_validate(metadata)
        # Convert back to dict, excluding unset/None values for cleaner storage
        # Use mode='json' to properly serialize enums, datetimes, etc.
        return validated.model_dump(exclude_none=True, exclude_unset=False, mode="json")
    except ValidationError as e:
        # Format a nice error message
        errors = []
        for error in e.errors():
            loc = ".".join(str(part) for part in error["loc"])
            msg = error["msg"]
            errors.append(f"  - {loc}: {msg}")

        error_msg = f"Invalid metadata for {type_str} memory:\n" + "\n".join(errors)
        raise ValueError(error_msg) from e


def get_metadata_schema(memory_type: str | MemoryType) -> dict[str, Any]:
    """Get the JSON schema for a memory type's metadata.

    Useful for documentation and client-side validation.

    Args:
        memory_type: The type of memory.

    Returns:
        JSON schema dict for the metadata model.

    Raises:
        ValueError: If the memory type is invalid.
    """
    # Normalize memory type to string
    if isinstance(memory_type, MemoryType):
        type_str = memory_type.value
    else:
        type_str = memory_type.lower()

    metadata_model = METADATA_MODELS_BY_STR.get(type_str)
    if metadata_model is None:
        raise ValueError(f"Invalid memory type: {memory_type}")

    return metadata_model.model_json_schema()


def get_metadata_field_descriptions(memory_type: str | MemoryType) -> dict[str, str]:
    """Get field descriptions for a memory type's metadata.

    Useful for UI hints and documentation.

    Args:
        memory_type: The type of memory.

    Returns:
        Dict mapping field names to their descriptions.

    Raises:
        ValueError: If the memory type is invalid.
    """
    # Normalize memory type to string
    if isinstance(memory_type, MemoryType):
        type_str = memory_type.value
    else:
        type_str = memory_type.lower()

    metadata_model = METADATA_MODELS_BY_STR.get(type_str)
    if metadata_model is None:
        raise ValueError(f"Invalid memory type: {memory_type}")

    descriptions = {}
    for name, field in metadata_model.model_fields.items():
        descriptions[name] = field.description or ""

    return descriptions


def _format_type_for_docs(field_info: FieldInfo, annotation: Any) -> str:
    """Format a field's type annotation for documentation."""
    origin = get_origin(annotation)
    args = get_args(annotation)

    # Handle Optional (Union with None)
    if origin is type(None) or annotation is type(None):
        return "null"

    # Handle Union types (e.g., str | None)
    if origin is type(str | int):  # UnionType
        non_none_args = [a for a in args if a is not type(None)]
        if len(non_none_args) == 1:
            return _format_type_for_docs(field_info, non_none_args[0])
        return " | ".join(_format_type_for_docs(field_info, a) for a in non_none_args)

    # Handle list types
    if origin is list:
        if args:
            inner = _format_type_for_docs(field_info, args[0])
            return f"[{inner}, ...]"
        return "[...]"

    # Handle dict types
    if origin is dict:
        return "{...}"

    # Handle enums
    if hasattr(annotation, "__members__"):
        values = [f'"{v.value}"' for v in annotation]
        return " | ".join(values)

    # Handle Pydantic models (nested objects)
    if hasattr(annotation, "model_fields"):
        return "{" + ", ".join(annotation.model_fields.keys()) + "}"

    # Basic types
    if annotation is str:
        return "string"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if annotation is bool:
        return "boolean"

    # datetime
    if hasattr(annotation, "__name__") and annotation.__name__ == "datetime":
        return "ISO datetime string"

    return str(annotation.__name__) if hasattr(annotation, "__name__") else str(annotation)


def _is_field_required(field_info: FieldInfo) -> bool:
    """Check if a field is required (no default value)."""
    return field_info.is_required()


def generate_metadata_docs_for_type(memory_type: str) -> str:
    """Generate LLM-friendly documentation for a single memory type's metadata.

    Args:
        memory_type: The memory type string.

    Returns:
        Formatted documentation string for this type's metadata.
    """
    metadata_model = METADATA_MODELS_BY_STR.get(memory_type)
    if metadata_model is None:
        return f"Unknown type: {memory_type}"

    lines = ["{"]

    for name, field_info in metadata_model.model_fields.items():
        annotation = metadata_model.__annotations__.get(name)
        type_str = _format_type_for_docs(field_info, annotation)
        desc = field_info.description or ""
        required = _is_field_required(field_info)
        req_marker = " (REQUIRED)" if required else ""

        lines.append(f'    "{name}": {type_str}{req_marker} - {desc},')

    lines.append("}")
    return "\n".join(lines)


def generate_all_metadata_docs() -> str:
    """Generate complete metadata documentation for all memory types.

    This is auto-generated from the Pydantic models, so it stays in sync
    with the actual validation rules.

    Returns:
        Complete documentation string for the create_memory tool.
    """
    docs = []

    type_descriptions = {
        "experience": "events, decisions, lessons learned",
        "technical": "code, solutions, patterns, technical knowledge",
        "procedural": "step-by-step processes, workflows",
        "goal": "objectives, milestones, progress tracking",
        "individual": "information about people",
    }

    for type_name, description in type_descriptions.items():
        docs.append(f'FOR type="{type_name}" ({description}):')
        docs.append(generate_metadata_docs_for_type(type_name))
        docs.append("")

    return "\n".join(docs)


# Pre-generate the documentation for use in tool docstrings
METADATA_DOCS = generate_all_metadata_docs()
