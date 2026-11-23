"""``python -m lucent.memory.backfill`` — embed memories that lack a current vector.

Migration 169 added the embedding columns, but only the write path fills them.
Rows written before semantic search existed are NULL, and a model change
invalidates every stored vector (``embedding_model`` no longer matches), so the
corpus needs a separate, resumable job. This is that job.

Strategy, and why it has this shape:

* **The columns are the progress marker.** A row is pending when it is live and
  either has no vector or one produced by a different model::

      deleted_at IS NULL AND (embedding IS NULL
                              OR embedding_model IS DISTINCT FROM $model)

  No cursor is kept in application state, so an interrupted run resumes simply
  by running again, and a completed run selects nothing — idempotent by
  construction. Switching ``memory.embedding_model`` makes the predicate pick
  up the whole corpus again.

* **Bounded batches, one short transaction each.** Encoding happens *outside*
  any transaction (it is the slow, CPU-bound part); a transaction wraps only the
  writes for one batch. An interruption loses at most one batch, and no
  transaction spans an inference call. Between batches other writers proceed
  normally.

* **Keyset pagination, not OFFSET.** Rows are walked in ``id`` order with
  ``id > last_id``, so the scan neither drifts as rows are inserted nor
  re-reads work already done. A row that fails to encode is skipped for the
  remainder of *this* run — one poison row must not spin forever — but the
  next run selects it again because its columns are still NULL/stale.

* **Only the three embedding columns change.** The UPDATE sets ``embedding``,
  ``embedding_model`` and ``embedded_at`` and nothing else. ``updated_at``
  drives decay and ordering, so it must not move: backfilling an old memory
  must not make it look freshly written. The base stamper
  (``update_memories_updated_at``) unconditionally assigns ``NEW.updated_at =
  NOW()`` on every UPDATE, so the batch transaction creates a temporary
  ``z_...`` BEFORE UPDATE trigger that restores ``OLD.updated_at``, runs the
  UPDATE, and drops it — all in the same transaction, following migration 163
  rather than 095's ``ALTER TABLE ... DISABLE TRIGGER``. The real trigger is
  never disabled or altered, and the temporary one cannot outlive a commit or
  rollback.

* **The write transaction is short and does not block readers.** Encoding
  happens outside any transaction. ``CREATE TRIGGER``/``DROP TRIGGER`` take
  ``SHARE ROW EXCLUSIVE`` on ``memories`` (not ``ACCESS EXCLUSIVE``), so
  concurrent ``SELECT``\\ s — the hot memory-search path — are never blocked;
  only concurrent writers wait, and only for the duration of one batch's single
  UPDATE round trip (no inference inside it), never for the whole run. No
  session-wide lock or long transaction is held. A predicate on
  ``updated_at`` plus the pending predicate makes a concurrent content edit or a
  concurrent embedding win the race rather than being overwritten with a stale
  vector.

* **Per-row failures are data, not errors.** The encoder is fail-soft
  (``embed_texts`` returns ``None`` per item); a ``None`` for non-empty content
  leaves that row untouched, increments ``failed``, is logged with its row id,
  and the run continues. Empty/whitespace content is counted separately — it
  can never be embedded, so it must not be reported as a persistent failure.
  A whole batch producing no usable vectors re-probes the encoder: a failing
  probe means the encoder went down mid-run (missing model, broken install) and
  the run aborts loudly; a passing probe means the rows themselves failed and
  the run continues, so one poison row — even alone in its batch — is skipped
  and retried later, never an abort.

This tool never deletes, rewrites or reorders memory content; it touches only
``embedding``, ``embedding_model`` and ``embedded_at``.

Exit codes:
    0  completed (possibly with skipped rows — see the summary)
    1  configuration error (no database URL / bad arguments)
    2  another backfill already holds the advisory lock
    3  runtime failure (database error, encoder unavailable for a whole batch)

Usage::

    python -m lucent.memory.backfill                 # DATABASE_URL from env
    python -m lucent.memory.backfill --dry-run       # count pending rows only
    python -m lucent.memory.backfill --batch-size 32
    python -m lucent.memory.backfill --limit 500     # bounded run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import asyncpg

from lucent.logging import configure_logging, get_logger
from lucent.memory.embeddings import (
    EMBEDDING_DIMENSIONS,
    embed_texts,
    embedding_model_id,
)

logger = get_logger("lucent.memory.backfill")

#: Rows per batch. Large enough that encoder throughput dominates per-batch
#: overhead, small enough that one interrupted batch is cheap to redo.
DEFAULT_BATCH_SIZE = 64
MAX_BATCH_SIZE = 1024

#: Stable 64-bit key for ``pg_try_advisory_lock``. Only needs to be unique
#: among this application's advisory locks (migrations use a different key).
ADVISORY_LOCK_KEY = 0x4C55434D454D42  # "LUCMEMB"

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_BUSY = 2
EXIT_FAILED = 3

#: Sync callable: texts in, one vector (or ``None``) per text out. Defaults to
#: the production encoder; injectable so tests and scratch runs never need the
#: real model.
Encoder = Callable[[Sequence[str]], list[list[float] | None]]

_PENDING_PREDICATE = (
    "deleted_at IS NULL AND (embedding IS NULL OR embedding_model IS DISTINCT FROM $1::text)"
)

_SELECT_BATCH_SQL = f"""
    SELECT id, content, updated_at
    FROM memories
    WHERE {_PENDING_PREDICATE}
      AND ($2::uuid IS NULL OR id > $2::uuid)
    ORDER BY id
    LIMIT $3
"""

_COUNT_PENDING_SQL = f"SELECT count(*) FROM memories WHERE {_PENDING_PREDICATE}"

# Preserve updated_at without disabling the real stamper (migration 163's
# technique, not 095's ALTER TABLE DISABLE TRIGGER). The function lives in
# pg_temp, so it is session-scoped and cannot outlive this connection; the
# trigger is created and dropped inside each batch transaction, so it cannot
# outlive a commit or rollback either. Same-event triggers fire in name order,
# and 'z_...' sorts after 'update_memories_updated_at', so the real stamper
# sets NOW() first and this restores the value it found.
_PRESERVE_FUNCTION_SQL = """
    CREATE OR REPLACE FUNCTION pg_temp.preserve_memories_updated_at()
    RETURNS trigger LANGUAGE plpgsql AS $fn$
    BEGIN
        NEW.updated_at = OLD.updated_at;
        RETURN NEW;
    END;
    $fn$
"""
_PRESERVE_TRIGGER_NAME = "z_backfill_embedding_preserve_updated_at"
_CREATE_PRESERVE_TRIGGER_SQL = f"""
    CREATE TRIGGER {_PRESERVE_TRIGGER_NAME}
        BEFORE UPDATE ON memories
        FOR EACH ROW EXECUTE FUNCTION pg_temp.preserve_memories_updated_at()
"""
_DROP_PRESERVE_TRIGGER_SQL = f"DROP TRIGGER {_PRESERVE_TRIGGER_NAME} ON memories"

# One round trip per batch. Multiple unnest() calls in the select list are
# zipped positionally by PostgreSQL, so id/vector/updated_at stay aligned.
_UPDATE_BATCH_SQL = """
    UPDATE memories AS m
    SET embedding = batch.embedding,
        embedding_model = $1::text,
        embedded_at = now()
    FROM (
        SELECT unnest($2::uuid[]) AS id,
               unnest($3::text[])::vector AS embedding,
               unnest($4::timestamptz[]) AS updated_at
    ) AS batch
    WHERE m.id = batch.id
      AND m.deleted_at IS NULL
      AND m.updated_at = batch.updated_at
      AND (m.embedding IS NULL OR m.embedding_model IS DISTINCT FROM $1::text)
    RETURNING m.id
"""


def _vector_literal(vector: Sequence[float]) -> str:
    """Render a Python vector as pgvector's text form (``[v1,v2,...]``).

    asyncpg has no codec for the ``vector`` type, so vectors cross the wire as
    text and are cast in SQL. The same 8-decimal form the memory write path
    uses (:func:`lucent.db.memory._vector_literal`) keeps stored and query
    vectors byte-comparable.
    """
    return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"


@dataclass
class _PendingWrite:
    """One row whose new vector is ready to be written."""

    id: Any
    vector_literal: str
    updated_at: datetime


@dataclass
class _ClassifiedBatch:
    writable: list[_PendingWrite] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    skipped_empty: int = 0

    @property
    def failed(self) -> int:
        return len(self.failed_ids)


def _classify_batch(
    rows: Sequence[Mapping[str, Any]],
    vectors: Sequence[Sequence[float] | None],
) -> _ClassifiedBatch:
    """Split a batch into writable rows, encoder failures and empty content.

    Pure: no database, no encoding. A ``None`` vector is a failure for
    non-empty content and an expected no-op for empty/whitespace content. A
    vector of the wrong width is treated as a failure too — writing it would
    either error in pgvector or corrupt the corpus, so the row stays pending.
    """
    result = _ClassifiedBatch()
    for row, vector in zip(rows, vectors, strict=True):
        content = row["content"]
        if vector is None or len(vector) != EMBEDDING_DIMENSIONS:
            if not content or not content.strip():
                result.skipped_empty += 1
            else:
                result.failed_ids.append(str(row["id"]))
            continue
        result.writable.append(
            _PendingWrite(
                id=row["id"],
                vector_literal=_vector_literal(vector),
                updated_at=row["updated_at"],
            )
        )
    return result


@dataclass
class BackfillReport:
    """Outcome of one run, shaped for both logs and machine consumption."""

    model_id: str
    remaining_at_start: int
    remaining_at_end: int
    fetched: int = 0
    written: int = 0
    failed: int = 0
    skipped_empty: int = 0
    skipped_concurrent: int = 0
    batches: int = 0
    elapsed_seconds: float = 0.0
    dry_run: bool = False

    @property
    def throughput_rows_per_second(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.written / self.elapsed_seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            "event": "memory_embedding_backfill_finished",
            "model_id": self.model_id,
            "dry_run": self.dry_run,
            "remaining_at_start": self.remaining_at_start,
            "remaining_at_end": self.remaining_at_end,
            "fetched": self.fetched,
            "written": self.written,
            "failed": self.failed,
            "skipped_empty": self.skipped_empty,
            "skipped_concurrent": self.skipped_concurrent,
            "batches": self.batches,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "throughput_rows_per_second": round(self.throughput_rows_per_second, 2),
        }


async def _probe_encoder(encoder: Encoder) -> bool:
    """Confirm the encoder can produce one correctly sized vector.

    Catches the expensive operator mistake — a missing model cache or a broken
    fastembed install — before any row is touched. Logs why it failed.
    """
    vectors = await asyncio.to_thread(encoder, ["lucent embedding backfill probe"])
    vector = vectors[0] if vectors else None
    if vector is None or len(vector) != EMBEDDING_DIMENSIONS:
        logger.error(
            "memory_embedding_backfill_encoder_unavailable %s",
            json.dumps(
                {
                    "event": "memory_embedding_backfill_encoder_unavailable",
                    "model_id": embedding_model_id(),
                    "expected_dimensions": EMBEDDING_DIMENSIONS,
                    "got_dimensions": None if vector is None else len(vector),
                }
            ),
        )
        return False
    return True


async def run_backfill(
    *,
    database_url: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    dry_run: bool = False,
    encoder: Encoder = embed_texts,
    model_id: str | None = None,
) -> BackfillReport:
    """Fill pending memory embeddings, batch by batch.

    Returns the run report; raises ``RuntimeError`` for a whole-batch encoder
    outage and lets database errors propagate. The caller owns the exit code.
    """
    model = model_id or embedding_model_id()
    started = time.perf_counter()
    conn = await asyncpg.connect(database_url)
    report = BackfillReport(
        model_id=model,
        remaining_at_start=0,
        remaining_at_end=0,
        dry_run=dry_run,
    )
    locked = False
    try:
        locked = bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_KEY))
        if not locked:
            raise BackfillBusyError("another memory embedding backfill holds the advisory lock")

        report.remaining_at_start = int(await conn.fetchval(_COUNT_PENDING_SQL, model))
        report.remaining_at_end = report.remaining_at_start
        logger.info(
            "memory_embedding_backfill_started %s",
            json.dumps(
                {
                    "event": "memory_embedding_backfill_started",
                    "model_id": model,
                    "pending": report.remaining_at_start,
                    "batch_size": batch_size,
                    "dry_run": dry_run,
                }
            ),
        )

        if dry_run:
            return report

        # Nothing pending is a successful no-op: do not load or probe the
        # encoder, so a fully-embedded corpus never depends on the model being
        # available and a re-run touches nothing.
        if report.remaining_at_start:
            if not await _probe_encoder(encoder):
                raise EncoderUnavailableError(
                    f"embedding encoder {model} produced no usable vector for a probe"
                )

            # Session-scoped function for the per-batch updated_at guard.
            # Created once; pg_temp objects disappear when this connection
            # closes.
            await conn.execute(_PRESERVE_FUNCTION_SQL)

        last_id = None
        while limit is None or report.written < limit:
            # Slice the last batch so --limit is exact rather than a rounded-up
            # batch boundary.
            batch_limit = batch_size if limit is None else min(batch_size, limit - report.written)
            rows = await conn.fetch(_SELECT_BATCH_SQL, model, last_id, batch_limit)
            if not rows:
                break

            texts = [row["content"] for row in rows]
            vectors = await asyncio.to_thread(encoder, texts)
            if len(vectors) != len(rows):
                raise RuntimeError(f"encoder returned {len(vectors)} vectors for {len(rows)} texts")

            classified = _classify_batch(rows, vectors)
            if classified.failed_ids:
                logger.warning(
                    "memory_embedding_backfill_rows_failed %s",
                    json.dumps(
                        {
                            "event": "memory_embedding_backfill_rows_failed",
                            "batch": report.batches + 1,
                            "failed": len(classified.failed_ids),
                            # Bounded so a large outage cannot flood the log.
                            "memory_ids": classified.failed_ids[:20],
                            "truncated": len(classified.failed_ids) > 20,
                        }
                    ),
                )
            written = 0
            if classified.writable:
                async with conn.transaction():
                    await conn.execute(_CREATE_PRESERVE_TRIGGER_SQL)
                    updated = await conn.fetch(
                        _UPDATE_BATCH_SQL,
                        model,
                        [str(item.id) for item in classified.writable],
                        [item.vector_literal for item in classified.writable],
                        [item.updated_at for item in classified.writable],
                    )
                    await conn.execute(_DROP_PRESERVE_TRIGGER_SQL)
                written = len(updated)

            report.batches += 1
            report.fetched += len(rows)
            report.written += written
            report.failed += classified.failed
            report.skipped_empty += classified.skipped_empty
            report.skipped_concurrent += len(classified.writable) - written
            report.remaining_at_end = max(0, report.remaining_at_start - report.fetched)

            # Every non-empty row in the batch failed. That is either an
            # encoder outage or a batch of individually bad rows. Re-probe to
            # tell them apart: a dead encoder aborts loudly; a live one means
            # these rows are skipped and retried on the next run (a single
            # poison row must never abort a run).
            if written == 0 and classified.failed == len(rows):
                if not await _probe_encoder(encoder):
                    raise EncoderUnavailableError(
                        f"encoder produced no usable vector for all {len(rows)} "
                        "rows in a batch and the probe now fails too; aborting"
                    )
                logger.warning(
                    "memory_embedding_backfill_batch_all_rows_failed %s",
                    json.dumps(
                        {
                            "event": "memory_embedding_backfill_batch_all_rows_failed",
                            "batch": report.batches,
                            "rows": len(rows),
                            "encoder_probe": "ok",
                        }
                    ),
                )

            elapsed = time.perf_counter() - started
            logger.info(
                "memory_embedding_backfill_progress %s",
                json.dumps(
                    {
                        "event": "memory_embedding_backfill_progress",
                        "batch": report.batches,
                        "remaining_estimate": report.remaining_at_end,
                        "processed": report.written,
                        "failed": report.failed,
                        "skipped_empty": report.skipped_empty,
                        "skipped_concurrent": report.skipped_concurrent,
                        "throughput_rows_per_second": round(report.written / elapsed, 2)
                        if elapsed > 0
                        else 0.0,
                    }
                ),
            )

            last_id = rows[-1]["id"]

        # Exact final count, so an estimate is never reported as truth.
        report.remaining_at_end = int(await conn.fetchval(_COUNT_PENDING_SQL, model))
        return report
    finally:
        if locked:
            try:
                await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
            except Exception:  # noqa: BLE001 — best effort on a dying connection
                logger.warning("memory_embedding_backfill_unlock_failed")
        await conn.close()
        report.elapsed_seconds = time.perf_counter() - started


class BackfillBusyError(RuntimeError):
    """Another backfill process holds the advisory lock."""


class EncoderUnavailableError(RuntimeError):
    """The encoder produced no usable vectors, so nothing could be embedded."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lucent.memory.backfill",
        description=(
            "Fill memories.embedding for rows that have no current-model vector. "
            "Resumable and idempotent: the embedding columns are the progress "
            "marker."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL connection URL. Defaults to $DATABASE_URL.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        metavar="N",
        help=f"Rows per batch (default {DEFAULT_BATCH_SIZE}, max {MAX_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after writing N rows (default: run to completion).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count pending rows and exit without encoding or writing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        logger.error("--batch-size must be between 1 and %d", MAX_BATCH_SIZE)
        return EXIT_CONFIG
    if args.limit is not None and args.limit < 1:
        logger.error("--limit must be at least 1")
        return EXIT_CONFIG

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is required (or pass --database-url)")
        return EXIT_CONFIG

    try:
        report = asyncio.run(
            run_backfill(
                database_url=database_url,
                batch_size=args.batch_size,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        )
    except BackfillBusyError as exc:
        logger.error("%s", exc)
        return EXIT_BUSY
    except EncoderUnavailableError as exc:
        logger.error("%s", exc)
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 — surface *why* to the operator
        logger.error("Memory embedding backfill failed: %s", exc, exc_info=True)
        return EXIT_FAILED

    payload = report.as_dict()
    logger.info("memory_embedding_backfill_finished %s", json.dumps(payload))
    print(json.dumps(payload, sort_keys=True), flush=True)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised as a module CLI
    sys.exit(main())
