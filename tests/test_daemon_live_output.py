"""Tests for the daemon's in-flight task output writer.

The writer's contract is narrow but load-bearing: it must never disturb a
model session, and it must never show a viewer a stale or truncated stream. The
tests below pin both halves of that contract.
"""

from __future__ import annotations

import pytest

from daemon.observability.live_output import (
    MAX_CONTENT,
    TaskLiveOutputWriter,
    build_live_output_writer,
)


class RecordingWriter(TaskLiveOutputWriter):
    """Writer whose database calls are captured instead of executed."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.executed: list[tuple[str, tuple]] = []
        self.fail_with: Exception | None = None

    async def _execute(self, sql, *args):  # type: ignore[override]
        if self.fail_with is not None:
            raise self.fail_with
        self.executed.append((sql, args))


def _writer(**overrides) -> RecordingWriter:
    options = {
        "task_id": "11111111-1111-1111-1111-111111111111",
        "organization_id": "22222222-2222-2222-2222-222222222222",
        "database_url": "postgresql://example/db",
    }
    options.update(overrides)
    return RecordingWriter(**options)


class TestEnablement:
    def test_writer_is_inert_without_a_task_id(self):
        writer = _writer(task_id=None)
        writer.append("hello")
        assert not writer.should_flush()
        assert writer._enabled is False

    def test_writer_is_inert_without_an_org(self):
        writer = _writer(organization_id=None)
        assert writer._enabled is False

    def test_only_the_tasks_primary_session_owns_the_row(self):
        # Continuations, fusion candidates, and fan-out passes run through the
        # same session runner; letting them all write would make the visible
        # stream flicker between unrelated agents.
        primary = build_live_output_writer(
            {
                "source": "daemon.task",
                "task_id": "abc",
                "organization_id": "org",
            },
            database_url="postgresql://example/db",
        )
        auxiliary = build_live_output_writer(
            {
                "source": "daemon.task_completion_continuation",
                "task_id": "abc",
                "organization_id": "org",
            },
            database_url="postgresql://example/db",
        )
        assert primary.task_id == "abc"
        assert auxiliary.task_id is None


class TestBuffering:
    def test_small_chunks_do_not_flush_immediately(self):
        writer = _writer()
        writer.append("hi")
        assert writer.should_flush() is False

    def test_a_large_backlog_flushes_without_waiting(self):
        writer = _writer()
        writer.append("x" * 1000)
        assert writer.should_flush() is True

    def test_buffer_is_bounded_so_one_stretch_cannot_grow_memory(self):
        writer = _writer()
        for _ in range(50):
            writer.append("y" * 2000)
        assert writer._buffered_chars <= MAX_CONTENT * 2

    def test_disabled_writer_does_not_accumulate(self):
        writer = _writer(task_id=None)
        writer.append("ignored")
        assert writer._buffer == []


class TestFlush:
    @pytest.mark.asyncio
    async def test_flush_appends_rather_than_replacing(self):
        writer = _writer()
        writer.append("first ")
        await writer.flush()
        writer.append("second")
        await writer.flush()

        assert len(writer.executed) == 2
        sql, args = writer.executed[1]
        # The SQL must concatenate with the existing row and keep a bounded
        # tail. Overwriting would leave a viewer seeing only the last chunk.
        assert "task_live_output.content || EXCLUDED.content" in sql
        assert "right(" in sql
        assert args[2] == "second"
        assert args[3] == MAX_CONTENT

    @pytest.mark.asyncio
    async def test_flush_clears_the_buffer(self):
        writer = _writer()
        writer.append("payload")
        await writer.flush()
        assert writer._buffer == []
        assert writer._buffered_chars == 0

    @pytest.mark.asyncio
    async def test_empty_flush_is_a_noop(self):
        writer = _writer()
        await writer.flush()
        assert writer.executed == []

    @pytest.mark.asyncio
    async def test_close_deletes_the_scratch_row(self):
        writer = _writer()
        writer.append("text")
        await writer.close()

        sql, args = writer.executed[-1]
        assert "DELETE FROM task_live_output" in sql
        assert args == (writer.task_id,)
        # Nothing further is written once closed.
        writer.append("after close")
        assert writer.should_flush() is False

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        writer = _writer()
        await writer.close()
        await writer.close()
        assert len(writer.executed) == 1

    @pytest.mark.asyncio
    async def test_transient_failure_does_not_permanently_disable_the_writer(self):
        """A dropped connection mid-run must not cost the rest of the stream."""
        writer = _writer()
        writer.append("payload")

        calls = {"n": 0}

        async def flaky(sql, *args):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("transient")
            writer.executed.append((sql, args))

        writer._execute = flaky  # type: ignore[assignment]
        await writer.flush()
        assert writer._failed is False

        writer.append("more")
        await writer.flush()
        assert writer.executed  # the retry succeeded

    @pytest.mark.asyncio
    async def test_repeated_failures_stop_the_writer(self):
        writer = _writer()
        writer._execute = _always_failing  # type: ignore[assignment]
        for _ in range(6):
            writer.append("payload")
            await writer.flush()
        assert writer._failed is True


async def _always_failing(sql, *args):
    raise ConnectionError("persistent")


class TestFlushScheduling:
    def test_a_scheduled_flush_does_not_queue_a_second_one(self):
        """Deltas arrive in bursts; one pending write is enough."""
        writer = _writer()
        writer.append("x" * 1000)
        assert writer.should_flush() is True
        writer._flush_scheduled = True
        assert writer.should_flush() is False
