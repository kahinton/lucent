"""Tests for shared agent prompt composition.

Verifies that granted skills are rendered as an on-demand reference list and
managed tools with their schemas, the single source of truth used by both the
daemon sub-agent path and the server chat path.
"""

from lucent.llm.agent_composition import (
    MANAGED_TOOLS_HEADER,
    SKILL_FETCH_TOOL,
    SKILLS_HEADER,
    managed_tool_names,
    render_managed_tools_section,
    render_skills_section,
    skill_names,
)


class TestRenderSkillsSection:
    def test_empty_returns_blank(self):
        assert render_skills_section([]) == ""
        assert render_skills_section(None) == ""

    def test_lists_name_id_description_not_content(self):
        skills = [
            {
                "id": "abc-123",
                "name": "dev-workflow",
                "description": "Standard development workflow",
                "content": "FULL BODY THAT SHOULD NOT BE INLINED",
            }
        ]
        out = render_skills_section(skills)
        assert SKILLS_HEADER in out
        assert "dev-workflow" in out
        assert "abc-123" in out
        assert "Standard development workflow" in out
        # The on-demand fetch tool must be referenced.
        assert SKILL_FETCH_TOOL in out
        # Full skill body must NOT be inlined.
        assert "FULL BODY THAT SHOULD NOT BE INLINED" not in out

    def test_multiple_skills_each_listed(self):
        skills = [
            {"id": "1", "name": "alpha", "description": "first"},
            {"id": "2", "name": "beta", "description": "second"},
        ]
        out = render_skills_section(skills)
        assert "alpha (skill_id: 1)" in out
        assert "beta (skill_id: 2)" in out

    def test_missing_description_still_renders(self):
        out = render_skills_section([{"id": "x", "name": "noop"}])
        assert "noop (skill_id: x)" in out


class TestRenderManagedToolsSection:
    def test_empty_returns_blank(self):
        assert render_managed_tools_section([]) == ""
        assert render_managed_tools_section(None) == ""

    def test_renders_tool_with_schema(self):
        tools = [
            {
                "name": "fetch_weather",
                "description": "Get a forecast",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
            }
        ]
        out = render_managed_tools_section(tools)
        assert MANAGED_TOOLS_HEADER in out
        assert "fetch_weather" in out
        assert "Get a forecast" in out
        assert "run_managed_tool" in out
        assert "input_schema" in out
        assert "city" in out


class TestNameHelpers:
    def test_skill_names(self):
        assert skill_names([{"name": "a"}, {"name": "b"}, {"id": "no-name"}]) == ["a", "b"]
        assert skill_names(None) == []

    def test_managed_tool_names(self):
        assert managed_tool_names([{"name": "t1"}, {}]) == ["t1"]
        assert managed_tool_names(None) == []
