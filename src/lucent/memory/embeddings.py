"""Text embeddings for semantic memory search.

The memory write path embeds every memory's ``content`` here; the query path
embeds the search query with the same model and compares vectors with cosine
distance (pgvector ``<=>``). Both sides MUST use
:data:`EMBEDDING_MODEL_ID` and the same normalisation, otherwise vectors from
different models (or different scaling) are not comparable — that identifier
is what is stored in ``memories.embedding_model``.

Design constraints:

* **Lazy and shared.** ``fastembed.TextEmbedding`` loads an ONNX model and is
  expensive to construct, so it is built once per (model, cache dir) and reused.
  Nothing is constructed at import time: importing this module must stay
  cheap and must not require the model to be present.
* **Thread-safe.** Construction and inference are serialised behind locks;
  callers may invoke these functions from worker threads.
* **Fail-open.** :func:`embed_text` returns ``None`` and :func:`embed_texts`
  returns ``None`` for every affected item, emitting one structured warning on
  failure. Neither ever raises, because a memory must never fail to save
  because its embedding could not be computed.
* **L2-normalised output.** The ranker uses cosine distance; we normalise
  explicitly so the contract does not depend on the library's default.
* **Offline by default.** The image bakes the model into
  ``memory.embedding_cache_dir`` (``/opt/models``) at build time, so
  encoding works with the network disabled. This module also provides the
  build-time hook (``python -m lucent.memory.embeddings --download``) that
  populates the cache and asserts the files exist.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from pathlib import Path
from typing import Any, Sequence

from lucent.settings import (
    DEFAULT_EMBEDDING_MODEL_NAME,
    memory_embedding_batch_size,
    memory_embedding_cache_dir,
    memory_embedding_model,
)

logger = logging.getLogger(__name__)

# ── Model identity ────────────────────────────────────────────────────────
#
# Single source of truth for the model, shared by the write path and (when it
# is wired) the query path. The value written to ``memories.embedding_model``
# is the canonical ``provider:model`` form below; ``BAAI/bge-small-en-v1.5``
# produces 384-dimensional, L2-normalisable English text embeddings.
EMBEDDING_PROVIDER = "fastembed"
DEFAULT_EMBEDDING_MODEL = DEFAULT_EMBEDDING_MODEL_NAME
EMBEDDING_MODEL_ID = f"{EMBEDDING_PROVIDER}:{DEFAULT_EMBEDDING_MODEL}"
EMBEDDING_DIMENSIONS = 384

# A model load failure is remembered for the process lifetime. Retrying a
# failed download on every memory write would turn one outage into a request
# storm; a restart (or a config change) clears it.
_embedder: Any | None = None
_embedder_key: tuple[str, str] | None = None
_embedder_failed_key: tuple[str, str] | None = None
_load_lock = threading.Lock()
_inference_lock = threading.Lock()


def embedding_model_name() -> str:
    """Configured fastembed model name, falling back to the baked default."""
    return memory_embedding_model() or DEFAULT_EMBEDDING_MODEL


def embedding_model_id() -> str:
    """Canonical ``provider:model`` identifier stored in ``embedding_model``.

    The write path and the query path both call this, so the compatibility
    key they filter on cannot drift from what was stored.
    """
    return f"{EMBEDDING_PROVIDER}:{embedding_model_name()}"


def _log_failure(operation: str, exc: BaseException, **context: Any) -> None:
    """Emit one structured warning line; never raises."""
    payload = {
        "event": "memory_embedding_failed",
        "operation": operation,
        "model_id": embedding_model_id(),
        **context,
        "error_type": type(exc).__name__,
        "error": str(exc)[:500],
    }
    logger.warning("memory_embedding_failed %s", json.dumps(payload, default=str))


def _get_embedder() -> Any | None:
    """Return the shared ``TextEmbedding``, constructing it on first use.

    Returns ``None`` (after logging) when the model cannot be loaded. The
    import itself is lazy so a missing/mispackaged dependency degrades to NULL
    embeddings instead of breaking imports of the memory package.
    """
    global _embedder, _embedder_key, _embedder_failed_key

    model_name = embedding_model_name()
    cache_dir = memory_embedding_cache_dir()
    key = (model_name, cache_dir)

    if _embedder is not None and _embedder_key == key:
        return _embedder
    if _embedder_failed_key == key:
        return None

    with _load_lock:
        # Another thread may have finished loading while we waited.
        if _embedder is not None and _embedder_key == key:
            return _embedder
        if _embedder_failed_key == key:
            return None
        try:
            from fastembed import TextEmbedding

            logger.info(
                "memory_embedding_model_loading %s",
                json.dumps(
                    {
                        "event": "memory_embedding_model_loading",
                        "model_id": embedding_model_id(),
                        "cache_dir": cache_dir,
                    }
                ),
            )
            embedder = TextEmbedding(
                model_name=model_name,
                cache_dir=cache_dir or None,
            )
        except Exception as exc:  # pragma: no cover - depends on environment
            _embedder_failed_key = key
            _log_failure(
                "model_load", exc, model_name=model_name, cache_dir=cache_dir
            )
            return None

        _embedder = embedder
        _embedder_key = key
        _embedder_failed_key = None
        logger.info(
            "memory_embedding_model_loaded %s",
            json.dumps(
                {
                    "event": "memory_embedding_model_loaded",
                    "model_id": embedding_model_id(),
                    "cache_dir": cache_dir,
                }
            ),
        )
        return embedder


def _l2_normalize(vector: Sequence[float]) -> list[float]:
    """Return ``vector`` scaled to unit L2 norm (unchanged if it is all zeros)."""
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm == 0.0:
        return values
    return [value / norm for value in values]


def embed_texts(texts: Sequence[str]) -> list[list[float] | None]:
    """Encode a batch, L2-normalised, with a per-item fail-soft result.

    The result always has the same length as ``texts``: each entry is the
    unit-norm vector for that text, or ``None`` when the text is
    empty/whitespace-only, when the encoder is unavailable, or when encoding
    failed. Never raises — a memory must not fail to save because its embedding
    could not be computed.
    """
    items = list(texts)
    if not items:
        return []

    results: list[list[float] | None] = [None] * len(items)
    positions = [
        index for index, item in enumerate(items) if item and item.strip()
    ]
    if not positions:
        return results

    embedder = _get_embedder()
    if embedder is None:
        return results

    payload = [items[index] for index in positions]
    try:
        with _inference_lock:
            raw_vectors = list(
                embedder.embed(payload, batch_size=memory_embedding_batch_size())
            )
        if len(raw_vectors) != len(payload):
            raise RuntimeError(
                f"embedding count mismatch: {len(raw_vectors)} vectors for "
                f"{len(payload)} texts"
            )
        for index, vector in zip(positions, raw_vectors):
            results[index] = _l2_normalize(vector)
    except Exception as exc:  # pragma: no cover - depends on library internals
        _log_failure("embed_batch", exc, batch_size=len(payload))
    return results


def embed_text(text: str) -> list[float] | None:
    """Encode one text, L2-normalised, or ``None`` on any failure.

    Returns ``None`` for empty/whitespace-only content (nothing is encoded) and
    whenever the encoder is unavailable; never raises. Failures are logged as a
    structured warning by :func:`embed_texts`.
    """
    if not text or not text.strip():
        return None
    vectors = embed_texts([text])
    if not vectors:
        return None
    return vectors[0]


def _reset_embedder() -> None:
    """Drop the cached encoder. Intended for tests and config changes."""
    global _embedder, _embedder_key, _embedder_failed_key
    with _load_lock:
        _embedder = None
        _embedder_key = None
        _embedder_failed_key = None


# ── Build-time hook ───────────────────────────────────────────────────────


def ensure_model_cached() -> Path:
    """Download the model into the configured cache and assert it is usable.

    Called from the Dockerfile after installing the package. Raises
    ``RuntimeError`` (-> non-zero exit) when the cache is empty, the ONNX
    weights are zero-length, or a verification encode does not produce a
    384-dimension vector. That turns a silent build-time fetch failure into a
    failed build instead of a broken first memory write in production.
    """
    cache_dir = Path(memory_embedding_cache_dir())
    if _get_embedder() is None:
        raise RuntimeError(
            f"could not load embedding model {embedding_model_id()} "
            f"(cache dir {cache_dir})"
        )

    onnx_files = sorted(cache_dir.rglob("*.onnx"))
    if not onnx_files:
        raise RuntimeError(
            f"embedding model files were not cached under {cache_dir} "
            f"for {embedding_model_id()}"
        )
    empty = [path for path in onnx_files if path.stat().st_size == 0]
    if empty:
        raise RuntimeError(f"zero-length ONNX model files: {empty}")

    vector = embed_text("build-time embedding verification")
    if vector is None or len(vector) != EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            f"embedding verification failed: expected a {EMBEDDING_DIMENSIONS}-"
            f"dimension vector, got {None if vector is None else len(vector)}"
        )

    logger.info(
        "memory_embedding_model_cached %s",
        json.dumps(
            {
                "event": "memory_embedding_model_cached",
                "model_id": embedding_model_id(),
                "cache_dir": str(cache_dir),
                "onnx_files": [str(path) for path in onnx_files],
                "total_bytes": sum(path.stat().st_size for path in onnx_files),
                "dimensions": len(vector),
            }
        ),
    )
    return cache_dir


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point used by the image build (``--download``)."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Populate and verify the baked embedding model cache."
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download the model into the configured cache and verify it encodes.",
    )
    args = parser.parse_args(argv)
    if not args.download:
        parser.print_help()
        return 2

    try:
        cache_dir = ensure_model_cached()
    except Exception as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1
    print(f"Embedding model {embedding_model_id()} cached in {cache_dir}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the image build
    raise SystemExit(main())
