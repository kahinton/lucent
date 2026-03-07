"""Tests for Lucent metadata validation utilities."""

import pytest

from lucent.models.memory import MemoryType, GoalStatus
from lucent.models.validation import (
    validate_metadata,
    get_metadata_schema,
    get_metadata_field_descriptions,
    generate_metadata_docs_for_type,
    generate_all_metadata_docs,
    METADATA_DOCS,
    METADATA_MODELS,
    METADATA_MODELS_BY_STR,
)


class TestValidateMetadata:
    """Tests for the validate_metadata function."""

    def test_none_metadata_returns_empty_dict(self):
        result = validate_metadata("experience", None)
        assert result == {}

    def test_empty_metadata_returns_empty_dict(self):
        result = validate_metadata("experience", {})
        assert result == {}

    def test_experience_valid_metadata(self):
        metadata = {
            "context": "Working on a project",
            "outcome": "Successfully completed",
            "lessons_learned": ["Always test first"],
            "related_entities": ["Lucent"],
        }
        result = validate_metadata("experience", metadata)
        assert result["context"] == "Working on a project"
        assert result["outcome"] == "Successfully completed"
        assert result["lessons_learned"] == ["Always test first"]

    def test_technical_valid_metadata(self):
        metadata = {
            "language": "python",
            "repo": "lucent",
            "filename": "server.py",
            "category": "architecture",
        }
        result = validate_metadata("technical", metadata)
        assert result["language"] == "python"
        assert result["repo"] == "lucent"

    def test_procedural_valid_metadata(self):
        metadata = {
            "steps": [
                {"order": 1, "description": "Step one"},
                {"order": 2, "description": "Step two", "notes": "Be careful"},
            ],
            "prerequisites": ["Python 3.12"],
            "estimated_time": "30 minutes",
        }
        result = validate_metadata("procedural", metadata)
        assert len(result["steps"]) == 2
        assert result["steps"][0]["order"] == 1

    def test_goal_valid_metadata(self):
        metadata = {
            "status": "active",
            "priority": 5,
            "blockers": ["Need more time"],
            "milestones": [{"description": "Phase 1", "status": "active"}],
        }
        result = validate_metadata("goal", metadata)
        assert result["status"] == "active"
        assert result["priority"] == 5

    def test_individual_valid_metadata(self):
        metadata = {
            "name": "John Doe",
            "relationship": "colleague",
            "organization": "Acme Corp",
            "role": "Engineer",
        }
        result = validate_metadata("individual", metadata)
        assert result["name"] == "John Doe"
        assert result["relationship"] == "colleague"

    def test_accepts_enum_type(self):
        result = validate_metadata(MemoryType.TECHNICAL, {"language": "rust"})
        assert result["language"] == "rust"

    def test_case_insensitive_type_string(self):
        result = validate_metadata("EXPERIENCE", {"context": "test"})
        assert result["context"] == "test"

    def test_invalid_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid memory type"):
            validate_metadata("nonexistent", {"key": "value"})

    def test_invalid_metadata_fields_raises_value_error(self):
        """Procedural steps require 'order' and 'description'."""
        with pytest.raises(ValueError, match="Invalid metadata"):
            validate_metadata("procedural", {
                "steps": [{"invalid_field": "bad"}],
            })

    def test_goal_priority_out_of_range(self):
        with pytest.raises(ValueError, match="Invalid metadata"):
            validate_metadata("goal", {"priority": 10})

    def test_excludes_none_values(self):
        """validate_metadata should exclude None fields from output."""
        result = validate_metadata("experience", {"context": "test"})
        # outcome was not provided, should not appear
        assert "outcome" not in result

    def test_preserves_empty_lists_from_defaults(self):
        """Default empty lists should be preserved since exclude_unset=False."""
        result = validate_metadata("experience", {"context": "test"})
        assert result["lessons_learned"] == []
        assert result["related_entities"] == []

    def test_individual_missing_required_name(self):
        """Individual metadata requires 'name' field."""
        with pytest.raises(ValueError, match="Invalid metadata"):
            validate_metadata("individual", {"relationship": "friend"})


class TestGetMetadataSchema:
    """Tests for the get_metadata_schema function."""

    def test_returns_json_schema(self):
        schema = get_metadata_schema("experience")
        assert "properties" in schema
        assert "context" in schema["properties"]

    def test_all_types_have_schemas(self):
        for type_name in ["experience", "technical", "procedural", "goal", "individual"]:
            schema = get_metadata_schema(type_name)
            assert isinstance(schema, dict)
            assert "properties" in schema

    def test_accepts_enum_type(self):
        schema = get_metadata_schema(MemoryType.GOAL)
        assert "properties" in schema
        assert "status" in schema["properties"]

    def test_invalid_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid memory type"):
            get_metadata_schema("nonexistent")


class TestGetMetadataFieldDescriptions:
    """Tests for the get_metadata_field_descriptions function."""

    def test_returns_field_descriptions(self):
        descs = get_metadata_field_descriptions("experience")
        assert "context" in descs
        assert isinstance(descs["context"], str)
        assert len(descs["context"]) > 0

    def test_technical_field_descriptions(self):
        descs = get_metadata_field_descriptions("technical")
        assert "language" in descs
        assert "repo" in descs
        assert "filename" in descs

    def test_accepts_enum_type(self):
        descs = get_metadata_field_descriptions(MemoryType.PROCEDURAL)
        assert "steps" in descs

    def test_invalid_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid memory type"):
            get_metadata_field_descriptions("nonexistent")


class TestGenerateMetadataDocs:
    """Tests for documentation generation functions."""

    def test_generate_docs_for_type(self):
        docs = generate_metadata_docs_for_type("experience")
        assert "context" in docs
        assert "outcome" in docs
        assert "{" in docs  # Should look like a JSON-ish structure

    def test_generate_docs_for_unknown_type(self):
        docs = generate_metadata_docs_for_type("nonexistent")
        assert "Unknown type" in docs

    def test_generate_all_metadata_docs(self):
        docs = generate_all_metadata_docs()
        # Should contain all memory types
        assert 'type="experience"' in docs
        assert 'type="technical"' in docs
        assert 'type="procedural"' in docs
        assert 'type="goal"' in docs
        assert 'type="individual"' in docs

    def test_metadata_docs_constant_is_populated(self):
        """METADATA_DOCS is pre-generated at import time."""
        assert isinstance(METADATA_DOCS, str)
        assert len(METADATA_DOCS) > 100


class TestMetadataModelMappings:
    """Tests for the metadata model mapping dicts."""

    def test_all_memory_types_have_model(self):
        for mt in MemoryType:
            assert mt in METADATA_MODELS, f"Missing model for {mt}"

    def test_string_keys_match_enum_values(self):
        for mt in MemoryType:
            assert mt.value in METADATA_MODELS_BY_STR, f"Missing string key for {mt.value}"

    def test_enum_and_string_maps_are_consistent(self):
        for mt in MemoryType:
            assert METADATA_MODELS[mt] is METADATA_MODELS_BY_STR[mt.value]
