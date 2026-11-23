"""Database-level tests for the memory embedding write path.

These are integration tests (they pull in ``db_pool``): they prove that
``MemoryRepository.create``/``update`` persist the embedding columns against a
real PostgreSQL with pgvector, and that an encoder failure cannot stop a
memory from being saved. They need the isolated test database to have the
``vector`` extension and migration 169 applied — ``init_db`` applies the whole
migration set, so a pgvector server is enough.

The encoder is always faked here: no model download and no inference.
"""

import logging

from lucent.db import MemoryRepository
from lucent.db import memory as memory_db
from lucent.memory import embeddings


class _FixedEmbedder:
    """Stand-in for ``fastembed.TextEmbedding`` returning one fixed vector."""

    def __init__(self, vector):
        self.vector = list(vector)

    def embed(self, texts, batch_size=32):
        for _ in texts:
            yield self.vector


def _unit_vector(position: int) -> list[float]:
    vector = [0.0] * embeddings.EMBEDDING_DIMENSIONS
    vector[position] = 1.0
    return vector


async def _create(repo, test_user, prefix, content):
    return await repo.create(
        username=f"{prefix}user",
        type="experience",
        content=content,
        tags=["embedding"],
        user_id=test_user["id"],
        organization_id=test_user["organization_id"],
    )


async def _embedding_row(db_pool, memory_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            """SELECT embedding::text AS embedding_text,
                      vector_dims(embedding) AS dimensions,
                      embedding_model,
                      embedded_at
               FROM memories WHERE id = $1""",
            str(memory_id),
        )


async def test_create_persists_384_dim_embedding_and_model_id(
    db_pool, test_user, clean_test_data, monkeypatch
):
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)
    monkeypatch.setattr(
        embeddings, "_get_embedder", lambda: _FixedEmbedder(_unit_vector(0))
    )

    repo = MemoryRepository(db_pool)
    prefix = clean_test_data
    memory = await _create(repo, test_user, prefix, f"{prefix}embedded memory")

    row = await _embedding_row(db_pool, memory["id"])
    assert row["dimensions"] == embeddings.EMBEDDING_DIMENSIONS == 384
    assert row["embedding_model"] == embeddings.EMBEDDING_MODEL_ID
    assert row["embedding_model"] == "fastembed:BAAI/bge-small-en-v1.5"
    assert row["embedded_at"] is not None
    assert row["embedding_text"].startswith("[1,0,0")

    # The returned row still comes from _FULL_COLUMNS and must not start
    # carrying the 384-float vector (or its provenance) to every caller.
    assert "embedding" not in memory
    assert "embedding_model" not in memory
    assert "embedded_at" not in memory


async def test_create_survives_encoder_exception_with_null_embedding(
    db_pool, test_user, clean_test_data, monkeypatch, caplog
):
    """Fail-open: a raising encoder must never prevent the memory from saving."""
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)

    def _boom(_text):
        raise RuntimeError("encoder exploded")

    monkeypatch.setattr(embeddings, "embed_text", _boom)

    repo = MemoryRepository(db_pool)
    prefix = clean_test_data
    with caplog.at_level(logging.WARNING, logger="lucent.db.memory"):
        memory = await _create(repo, test_user, prefix, f"{prefix}unembeddable")

    # The memory is saved and readable.
    stored = await repo.get(
        memory["id"], organization_id=test_user["organization_id"]
    )
    assert stored is not None
    assert stored["content"] == f"{prefix}unembeddable"

    row = await _embedding_row(db_pool, memory["id"])
    assert row["embedding_text"] is None
    assert row["dimensions"] is None
    assert row["embedding_model"] is None
    assert row["embedded_at"] is None
    assert "memory_embedding_write_failed" in caplog.text
    assert any(
        getattr(record, "error", None) == "encoder exploded"
        for record in caplog.records
        if record.name == "lucent.db.memory"
    )


async def test_create_survives_encoder_returning_none(
    db_pool, test_user, clean_test_data, monkeypatch
):
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)
    monkeypatch.setattr(embeddings, "embed_text", lambda _text: None)

    repo = MemoryRepository(db_pool)
    prefix = clean_test_data
    memory = await _create(repo, test_user, prefix, f"{prefix}no vector")

    row = await _embedding_row(db_pool, memory["id"])
    assert row["embedding_text"] is None
    assert row["embedding_model"] is None
    assert row["embedded_at"] is None


async def test_update_content_refreshes_embedding(
    db_pool, test_user, clean_test_data, monkeypatch
):
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)
    monkeypatch.setattr(
        embeddings, "_get_embedder", lambda: _FixedEmbedder(_unit_vector(0))
    )

    repo = MemoryRepository(db_pool)
    prefix = clean_test_data
    memory = await _create(repo, test_user, prefix, f"{prefix}original text")
    before = await _embedding_row(db_pool, memory["id"])

    # A different text yields a different vector.
    monkeypatch.setattr(
        embeddings, "_get_embedder", lambda: _FixedEmbedder(_unit_vector(1))
    )
    updated = await repo.update(
        memory_id=memory["id"],
        content=f"{prefix}original text, now revised",
        organization_id=test_user["organization_id"],
        user_id=test_user["id"],
    )
    assert updated is not None

    after = await _embedding_row(db_pool, memory["id"])
    assert after["dimensions"] == 384
    assert after["embedding_model"] == embeddings.EMBEDDING_MODEL_ID
    assert after["embedding_text"] != before["embedding_text"]
    assert after["embedding_text"].startswith("[0,1,0")
    assert after["embedded_at"] >= before["embedded_at"]


async def test_update_without_content_leaves_embedding_untouched(
    db_pool, test_user, clean_test_data, monkeypatch
):
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)
    monkeypatch.setattr(
        embeddings, "_get_embedder", lambda: _FixedEmbedder(_unit_vector(0))
    )

    repo = MemoryRepository(db_pool)
    prefix = clean_test_data
    memory = await _create(repo, test_user, prefix, f"{prefix}stable text")
    before = await _embedding_row(db_pool, memory["id"])

    await repo.update(
        memory_id=memory["id"],
        tags=["changed"],
        organization_id=test_user["organization_id"],
        user_id=test_user["id"],
    )

    after = await _embedding_row(db_pool, memory["id"])
    assert after["embedding_text"] == before["embedding_text"]
    assert after["embedding_model"] == before["embedding_model"]
    assert after["embedded_at"] == before["embedded_at"]
