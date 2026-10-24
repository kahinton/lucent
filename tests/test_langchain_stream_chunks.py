"""Streamed-chunk text extraction for the LangChain engine.

Providers disagree about what a streamed chunk's ``content`` looks like. The
engine previously assumed a plain string, which silently dropped every chunk
from block-based providers — the final answer still assembled correctly, so the
only visible symptom was that nothing ever streamed. These tests pin both
shapes.
"""

from __future__ import annotations

from lucent.llm.langchain_engine import _stream_chunk_text


class TestStringContent:
    def test_plain_string_passes_through(self):
        assert _stream_chunk_text("hello") == "hello"

    def test_empty_string(self):
        assert _stream_chunk_text("") == ""


class TestBlockContent:
    def test_text_blocks_are_concatenated(self):
        content = [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ]
        assert _stream_chunk_text(content) == "Hello world"

    def test_reasoning_blocks_are_excluded(self):
        """Reasoning is not the answer; mixing it in would misrepresent output."""
        content = [
            {"type": "reasoning", "text": "secret thinking"},
            {"type": "text", "text": "visible"},
        ]
        assert _stream_chunk_text(content) == "visible"

    def test_plain_string_blocks_are_included(self):
        assert _stream_chunk_text(["a", "b"]) == "ab"

    def test_untagged_blocks_with_text_are_included(self):
        assert _stream_chunk_text([{"text": "bare"}]) == "bare"

    def test_blocks_without_text_are_ignored(self):
        content = [{"type": "tool_use", "name": "search"}, {"type": "text", "text": "ok"}]
        assert _stream_chunk_text(content) == "ok"


class TestOtherShapes:
    def test_none_is_empty(self):
        assert _stream_chunk_text(None) == ""

    def test_unknown_type_is_empty(self):
        assert _stream_chunk_text(42) == ""
