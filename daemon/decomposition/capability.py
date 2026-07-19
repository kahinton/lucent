"""Composable request-decomposition helpers for the daemon runtime."""

from __future__ import annotations

from typing import Any

from daemon.decomposition import fallback


class DecompositionHelpersMixin:
    """Pure prompt and fallback construction used by decomposition orchestration."""

    def _build_decomposition_prompt(self, request: dict[str, Any]) -> str:
        return fallback.build_decomposition_prompt(request)

    @staticmethod
    def _extract_suggested_breakdown_items(description: str) -> list[str]:
        return fallback.extract_suggested_breakdown_items(description)

    @staticmethod
    def _strip_suggested_breakdown_section(description: str) -> str:
        return fallback.strip_suggested_breakdown_section(description)

    @staticmethod
    def _fallback_agent_type_for_decomposition_item(item: str) -> str:
        return fallback.fallback_agent_type(item)

    @staticmethod
    def _fallback_task_title(item: str) -> str:
        return fallback.fallback_task_title(item)

    def _build_fallback_decomposition_tasks(
        self, request: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return fallback.build_fallback_tasks(request)
