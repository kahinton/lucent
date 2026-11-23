"""Unit tests for the memory embedding encoder and its write-path hook.

These never download or run the real model: the shared encoder is replaced
with a fake, so the suite stays fast and works offline. Real-model behaviour is
verified in the image build (``python -m lucent.memory.embeddings --download``).
"""

import math

import pytest

from lucent.db import memory as memory_db
from lucent.memory import embeddings


class _FakeEmbedder:
    def __init__(self, vectors):
        self.vectors = vectors
        self.calls: list[tuple[list[str], int]] = []

    def embed(self, texts, batch_size=32):
        self.calls.append((list(texts), batch_size))
        yield from self.vectors


def test_model_id_is_canonical_and_shared():
    assert embeddings.EMBEDDING_MODEL_ID == "fastembed:BAAI/bge-small-en-v1.5"
    assert embeddings.embedding_model_id() == embeddings.EMBEDDING_MODEL_ID
    assert embeddings.EMBEDDING_DIMENSIONS == 384


def test_embed_texts_l2_normalises(monkeypatch):
    fake = _FakeEmbedder([[3.0, 4.0], [0.0, 0.0]])
    monkeypatch.setattr(embeddings, "_get_embedder", lambda: fake)

    vectors = embeddings.embed_texts(["a", "b"])

    assert vectors is not None
    assert vectors[0] == pytest.approx([0.6, 0.8])
    assert vectors[1] == [0.0, 0.0]  # all-zero stays zero instead of dividing by 0
    assert math.isclose(math.sqrt(sum(value * value for value in vectors[0])), 1.0)
    assert fake.calls == [(["a", "b"], embeddings.memory_embedding_batch_size())]


def test_embed_text_returns_none_and_logs_on_failure(monkeypatch, caplog):
    class _Boom:
        def embed(self, texts, batch_size=32):
            raise RuntimeError("onnx exploded")

    monkeypatch.setattr(embeddings, "_get_embedder", lambda: _Boom())
    with caplog.at_level("WARNING"):
        assert embeddings.embed_text("hello") is None

    assert "memory_embedding_failed" in caplog.text
    assert "onnx exploded" in caplog.text


def test_encoder_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr(embeddings, "_get_embedder", lambda: None)
    assert embeddings.embed_text("hello") is None
    assert embeddings.embed_texts(["hello"]) == [None]


def test_embed_texts_skips_empty_items_per_position(monkeypatch):
    fake = _FakeEmbedder([[3.0, 4.0]])
    monkeypatch.setattr(embeddings, "_get_embedder", lambda: fake)

    vectors = embeddings.embed_texts(["", "memory decay", "   "])

    assert vectors is not None
    assert len(vectors) == 3
    assert vectors[0] is None
    assert vectors[2] is None
    assert vectors[1] == pytest.approx([0.6, 0.8])
    # Only the non-empty text is handed to the model.
    assert fake.calls == [(["memory decay"], embeddings.memory_embedding_batch_size())]


def test_embed_texts_batch_failure_returns_none_per_item(monkeypatch, caplog):
    class _Boom:
        def embed(self, texts, batch_size=32):
            raise RuntimeError("onnx exploded")

    monkeypatch.setattr(embeddings, "_get_embedder", lambda: _Boom())
    with caplog.at_level("WARNING"):
        assert embeddings.embed_texts(["a", "b"]) == [None, None]

    assert "memory_embedding_failed" in caplog.text


def test_empty_content_is_not_an_error(monkeypatch):
    def _fail():
        raise AssertionError("empty content must not construct the model")

    monkeypatch.setattr(embeddings, "_get_embedder", _fail)
    assert embeddings.embed_text("") is None
    assert embeddings.embed_text("   ") is None


async def test_embedding_for_write_disabled(monkeypatch):
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: False)
    assert await memory_db._embedding_for_write("anything") == (None, None, None)


async def test_embedding_for_write_is_fail_open(monkeypatch, caplog):
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)

    def _boom(_text):
        raise RuntimeError("encoder unavailable")

    monkeypatch.setattr(embeddings, "embed_text", _boom)
    with caplog.at_level("WARNING"):
        result = await memory_db._embedding_for_write("anything")

    assert result == (None, None, None)
    assert "memory_embedding_write_failed" in caplog.text


async def test_embedding_for_write_populates_vector(monkeypatch):
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)
    vector = [0.0] * embeddings.EMBEDDING_DIMENSIONS
    vector[0] = 1.0
    monkeypatch.setattr(embeddings, "embed_text", lambda _text: vector)

    literal, model_id, embedded_at = await memory_db._embedding_for_write("anything")

    assert literal is not None
    assert literal.startswith("[1.00000000,0.00000000")
    assert literal.count(",") == embeddings.EMBEDDING_DIMENSIONS - 1
    assert model_id == embeddings.EMBEDDING_MODEL_ID
    assert embedded_at is not None


async def test_embedding_for_write_rejects_wrong_dimensions(monkeypatch, caplog):
    """A vector the column cannot hold must become NULL, never a failed write."""
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)
    monkeypatch.setattr(embeddings, "embed_text", lambda _text: [0.6, 0.8])

    with caplog.at_level("WARNING"):
        result = await memory_db._embedding_for_write("anything")

    assert result == (None, None, None)
    assert "memory_embedding_write_rejected" in caplog.text


async def test_embedding_for_write_rejects_non_finite_components(monkeypatch, caplog):
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)
    vector = [0.0] * embeddings.EMBEDDING_DIMENSIONS
    vector[7] = float("nan")
    monkeypatch.setattr(embeddings, "embed_text", lambda _text: vector)

    with caplog.at_level("WARNING"):
        result = await memory_db._embedding_for_write("anything")

    assert result == (None, None, None)
    assert "memory_embedding_write_rejected" in caplog.text
