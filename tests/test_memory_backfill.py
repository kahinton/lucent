"""Tests for the resumable memory embedding backfill.

The pure classification/formatting logic and the CLI surface run without a
connection. Database behaviour — completion, idempotency, skip-and-retry,
coverage and paraphrase retrieval after a run — is proved against the scratch
pgvector database (``scripts/dev-test-db.sh``) with a deterministic fake
encoder, so no model is downloaded and no inference runs.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from lucent.db import MemoryRepository
from lucent.db import memory as memory_db
from lucent.memory import backfill, embeddings
from lucent.memory.backfill import run_backfill
from lucent.memory.embeddings import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL_ID

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _row(row_id: str, content: str) -> dict:
    return {"id": row_id, "content": content, "updated_at": NOW}


def _vector(seed: float = 1.0) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[0] = seed
    return vector


def test_vector_literal_uses_pgvector_text_form():
    assert backfill._vector_literal([1.5, 0.25]) == "[1.50000000,0.25000000]"


def test_pending_predicate_selects_null_and_stale_models():
    predicate = backfill._PENDING_PREDICATE
    assert "deleted_at IS NULL" in predicate
    assert "embedding IS NULL" in predicate
    assert "embedding_model IS DISTINCT FROM" in predicate


def test_classify_batch_separates_writable_failed_and_empty():
    rows = [
        _row("a", "memory decay is expected"),
        _row("b", "encoder cannot handle this"),
        _row("c", ""),
        _row("d", "   "),
    ]
    vectors = [_vector(), None, None, None]

    batch = backfill._classify_batch(rows, vectors)

    assert [item.id for item in batch.writable] == ["a"]
    assert batch.failed == 1
    assert batch.failed_ids == ["b"]
    assert batch.skipped_empty == 2
    assert batch.writable[0].updated_at == NOW
    assert batch.writable[0].vector_literal.startswith("[1.00000000,")


def test_classify_batch_treats_wrong_width_vector_as_failure():
    batch = backfill._classify_batch([_row("a", "a real memory")], [[1.0, 2.0, 3.0]])
    assert batch.writable == []
    assert batch.failed == 1


def test_classify_batch_requires_one_vector_per_row():
    with pytest.raises(ValueError):
        backfill._classify_batch([_row("a", "x")], [])


def test_report_throughput_and_payload():
    report = backfill.BackfillReport(
        model_id="fastembed:test",
        remaining_at_start=100,
        remaining_at_end=0,
        written=100,
        elapsed_seconds=10.0,
    )
    assert report.throughput_rows_per_second == pytest.approx(10.0)
    payload = report.as_dict()
    assert payload["event"] == "memory_embedding_backfill_finished"
    assert payload["written"] == 100
    assert payload["throughput_rows_per_second"] == pytest.approx(10.0)


def test_parser_defaults():
    args = backfill._build_parser().parse_args([])
    assert args.batch_size == backfill.DEFAULT_BATCH_SIZE
    assert args.limit is None
    assert args.dry_run is False


def test_main_returns_config_error_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert backfill.main([]) == backfill.EXIT_CONFIG


def test_main_rejects_out_of_range_batch_size(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    assert backfill.main(["--batch-size", "0"]) == backfill.EXIT_CONFIG
    assert backfill.main(["--batch-size", "99999"]) == backfill.EXIT_CONFIG


# ---------------------------------------------------------------------------
# Database proof (integration): scratch pgvector DB, deterministic fake encoder
# ---------------------------------------------------------------------------


def _unit(position: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[position] = 1.0
    return vector


class _ScratchEncoder:
    """Deterministic sync stand-in for ``fastembed.TextEmbedding``.

    ``poison`` texts return ``None`` — the same per-item shape the real
    fail-soft encoder produces for a row it cannot encode. Every other
    non-empty text is mapped by ``resolver``; the default places every text on
    one axis so a paraphrase query matches.
    """

    def __init__(self, *, poison: set[str] | None = None, resolver=None):
        self.poison = set(poison or ())
        self.resolver = resolver or (lambda _text: _unit(0))
        self.batches: list[int] = []

    def __call__(self, texts):
        items = list(texts)
        self.batches.append(len(items))
        return [
            None if (not text or not text.strip() or text in self.poison) else self.resolver(text)
            for text in items
        ]


def _database_url() -> str:
    return os.environ["DATABASE_URL"]


@pytest_asyncio.fixture
async def seed_unembedded_memory(db_pool, test_user, monkeypatch):
    """Seed an unembedded memory (write-path embedding disabled for the call)."""
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: False)
    repo = MemoryRepository(db_pool)

    async def _seed(content: str) -> dict:
        return await repo.create(
            username=None,
            type="experience",
            content=content,
            tags=["backfill-scratch"],
            importance=5,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

    return _seed


async def _snapshot(db_pool, memory_ids):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, content, updated_at,
                      embedding::text AS embedding_text,
                      vector_dims(embedding) AS dimensions,
                      embedding_model, embedded_at
               FROM memories WHERE id = ANY($1::uuid[])""",
            [str(memory_id) for memory_id in memory_ids],
        )
    return {str(row["id"]): dict(row) for row in rows}


async def test_backfill_completes_and_then_processes_zero_rows(
    db_pool, test_user, seed_unembedded_memory
):
    """Run to completion, then prove the second run is a no-op."""
    seeded = [
        await seed_unembedded_memory(f"scratch backfill completion row {index}")
        for index in range(25)
    ]
    seeded_ids = [memory["id"] for memory in seeded]

    before = await _snapshot(db_pool, seeded_ids)
    # A backfill must never move updated_at: freshness drives decay/ordering.
    assert all(row["embedding_text"] is None for row in before.values())

    encoder = _ScratchEncoder()
    first = await run_backfill(
        database_url=_database_url(), batch_size=7, encoder=encoder
    )

    # Batches are bounded and the run converges on the whole scratch corpus.
    assert first.batches >= 4
    assert all(size <= 7 for size in encoder.batches)
    assert first.failed == 0
    assert first.remaining_at_end == 0
    assert first.written >= len(seeded_ids)

    after = await _snapshot(db_pool, seeded_ids)
    for memory_id in map(str, seeded_ids):
        row = after[memory_id]
        assert row["embedding_text"] is not None
        assert row["dimensions"] == EMBEDDING_DIMENSIONS
        assert row["embedding_model"] == EMBEDDING_MODEL_ID
        assert row["embedded_at"] is not None
        # Content and updated_at are byte-identical to the pre-run snapshot.
        assert row["content"] == before[memory_id]["content"]
        assert row["updated_at"] == before[memory_id]["updated_at"]

    # Coverage verification query, scoped to this test's organization.
    async with db_pool.acquire() as conn:
        covered, non_deleted = await conn.fetchrow(
            """SELECT count(*) FILTER (
                          WHERE embedding IS NOT NULL
                            AND embedding_model = $1
                      ) AS covered,
                      count(*) AS non_deleted
               FROM memories
               WHERE deleted_at IS NULL AND organization_id = $2""",
            EMBEDDING_MODEL_ID,
            test_user["organization_id"],
        )
    # Every live row in this organization is covered — including the individual
    # memory UserRepository.create seeds automatically — and every seeded row is
    # among them. (No empty content, so nothing is legitimately unembeddable.)
    assert covered == non_deleted
    assert covered >= len(seeded_ids)
    assert non_deleted == len(seeded_ids) + 1

    # Second run: the embedding columns are the progress marker, so nothing
    # is pending and the encoder is never invoked.
    second_encoder = _ScratchEncoder()
    second = await run_backfill(
        database_url=_database_url(), batch_size=7, encoder=second_encoder
    )
    assert second.remaining_at_start == 0
    assert second.fetched == 0
    assert second.written == 0
    assert second.failed == 0
    assert second.batches == 0
    assert second_encoder.batches == []


async def test_failed_row_is_skipped_and_retried_on_the_next_run(
    db_pool, test_user, seed_unembedded_memory
):
    """A per-row encoder failure is data: skipped now, retried later."""
    healthy = [
        await seed_unembedded_memory(f"scratch backfill healthy row {index}")
        for index in range(5)
    ]
    poison = await seed_unembedded_memory("scratch backfill POISON cannot encode")
    poison_content = poison["content"]

    first_encoder = _ScratchEncoder(poison={poison_content})
    first = await run_backfill(
        database_url=_database_url(), batch_size=64, encoder=first_encoder
    )

    assert first.failed >= 1
    assert first.written >= len(healthy)
    # The poison row is still pending, so it is counted, not silently done.
    assert first.remaining_at_end >= 1

    rows = await _snapshot(db_pool, [memory["id"] for memory in healthy] + [poison["id"]])
    for memory in healthy:
        assert rows[str(memory["id"])]["embedding_model"] == EMBEDDING_MODEL_ID
    failed_row = rows[str(poison["id"])]
    assert failed_row["embedding_text"] is None
    assert failed_row["embedding_model"] is None
    assert failed_row["embedded_at"] is None

    # A later pass with a working encoder retries exactly the leftover row.
    retry_encoder = _ScratchEncoder()
    retry = await run_backfill(
        database_url=_database_url(), batch_size=64, encoder=retry_encoder
    )
    assert retry.failed == 0
    assert retry.remaining_at_end == 0
    retried = (await _snapshot(db_pool, [poison["id"]]))[str(poison["id"])]
    assert retried["embedding_text"] is not None
    assert retried["embedding_model"] == EMBEDDING_MODEL_ID


async def test_lone_failed_row_does_not_abort_the_run(
    db_pool, test_user, seed_unembedded_memory
):
    """A row alone in its batch with no vector must be skipped, not fatal.

    This is the edge that distinguishes a per-row failure from an encoder
    outage: the batch is one row, every row in it failed, yet the encoder's
    probe still works, so the run must continue and leave the row for later.
    """
    poison = await seed_unembedded_memory("scratch backfill LONE POISON row")

    # Embed everything pending first, so the poison can be the only leftover.
    await run_backfill(
        database_url=_database_url(), batch_size=64, encoder=_ScratchEncoder()
    )
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE memories
               SET embedding = NULL, embedding_model = NULL, embedded_at = NULL
               WHERE id = $1""",
            str(poison["id"]),
        )

    report = await run_backfill(
        database_url=_database_url(),
        batch_size=64,
        encoder=_ScratchEncoder(poison={poison["content"]}),
    )

    assert report.batches == 1
    assert report.fetched == 1
    assert report.written == 0
    assert report.failed == 1
    assert report.remaining_at_end == 1
    row = (await _snapshot(db_pool, [poison["id"]]))[str(poison["id"])]
    assert row["embedding_text"] is None
    assert row["embedding_model"] is None


async def test_paraphrase_retrieval_works_after_backfill(
    db_pool, test_user, seed_unembedded_memory, monkeypatch
):
    """The previous task's paraphrase recall now works because rows are embedded."""
    memory = await seed_unembedded_memory(
        "gradual memory decay is expected during long sessions"
    )
    repo = MemoryRepository(db_pool)

    def resolver(text: str) -> list[float]:
        lowered = text.lower()
        # The paraphrase and the stored wording land on the same axis.
        return _unit(0) if ("decay" in lowered or "forgetting" in lowered) else _unit(1)

    monkeypatch.setattr(embeddings, "embed_text", lambda text: resolver(text))
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)

    async def _search():
        return await repo.search(
            query="forgetting",
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
            limit=10,
        )

    # Before the backfill the row is unembedded: lexical-only, so a paraphrase
    # that shares no term with the content is not recalled.
    before = await _search()
    assert before["total_count"] == 0

    report = await run_backfill(
        database_url=_database_url(),
        batch_size=64,
        encoder=_ScratchEncoder(resolver=resolver),
    )
    assert report.remaining_at_end == 0

    # After the backfill the vector arm admits it.
    after = await _search()
    assert memory["id"] in {row["id"] for row in after["memories"]}
