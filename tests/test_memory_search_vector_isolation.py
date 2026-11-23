"""Isolation and ranking tests for the semantic (pgvector) memory search arm.

The vector arm is only correct if it is *inside* the same clearance-scoped
statement that the lexical arm runs in. A parallel or earlier
``ORDER BY embedding <=> $1 LIMIT n`` would let another tenant's rows occupy
the candidate window: that destroys the caller's recall and, if the result
window were filtered later, leaks rows. These tests make that claim real.

They come in three groups:

- **SQL-shape tests** (no database): a real ``AuthorizedDatabasePool`` rewrites
  the emitted statement and the captured SQL is asserted to carry both the
  ``<=>`` expression and the ``auth_clearances`` scope. A meta-test feeds the
  same assertion an unscoped statement to prove the guard can fail.
- **Database tests** (integration, scratch pgvector DB on port 5434): real
  users, real rows, a faked encoder so similarity is deterministic. They prove
  cross-user invisibility, LIMIT behaviour under a foreign corpus, graceful
  fallback, model-mismatch handling, exact-identifier priority, paraphrase
  recall, and write-path fail-open.
- **Fail-before witnesses** for the database claims: the same corpus and the
  same ``MemoryRepository.search()`` are run with the clearance rewrite
  disabled, so the foreign row/LIMIT behaviour is observed rather than
  assumed. The isolation assertions would fail against that defect.

The encoder is always faked (``embeddings.embed_text``): no model download and
no inference. ``scripts/dev-test-db.sh`` provisions the isolated
``pgvector/pgvector:pg16`` container; tests/conftest.py refuses any URL aimed
at the live database.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest
import pytest_asyncio

from lucent.db import MemoryRepository, UserRepository
from lucent.db import memory as memory_db
from lucent.db.pool import (
    AuthorizedDatabasePool,
    AuthPrincipal,
)
from lucent.memory import embeddings

MODEL_ID = embeddings.EMBEDDING_MODEL_ID
DIMENSIONS = embeddings.EMBEDDING_DIMENSIONS
STALE_MODEL_ID = "fastembed:retired-model-v0"


# ---------------------------------------------------------------------------
# Deterministic vectors
# ---------------------------------------------------------------------------


def _unit(position: int) -> list[float]:
    vector = [0.0] * DIMENSIONS
    vector[position] = 1.0
    return vector


def _cos_vector(cosine: float) -> list[float]:
    """Unit vector whose cosine similarity with ``_unit(0)`` is ``cosine``."""
    complement = max(0.0, 1.0 - cosine * cosine) ** 0.5
    vector = [0.0] * DIMENSIONS
    vector[0] = cosine
    vector[1] = complement
    return vector


def _literal(vector: list[float]) -> str:
    """pgvector text form, via the production helper under test."""
    return memory_db._vector_literal(vector)


def _patch_encoder(monkeypatch, resolver):
    """Route every embed through ``resolver`` and force the vector flag on.

    Returns the list of texts handed to the encoder so tests can assert the
    query arm actually ran (rather than a broken arm silently returning zero).
    """
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)
    calls: list[str] = []

    def _embed(text: str) -> list[float] | None:
        calls.append(text)
        return resolver(text)

    monkeypatch.setattr(embeddings, "embed_text", _embed)
    return calls


async def _create(
    repo: MemoryRepository,
    user: dict,
    content: str,
    *,
    shared: bool = False,
) -> dict:
    return await repo.create(
        username=None,
        type="experience",
        content=content,
        tags=["vector-isolation"],
        importance=5,
        user_id=user["id"],
        organization_id=user["organization_id"],
        shared=shared,
    )


async def _embedding_similarity(db_pool, memory_id, vector_literal: str) -> float:
    """Cosine similarity between a stored row and a query vector, from SQL."""
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT 1 - (embedding <=> $2::vector) FROM memories WHERE id = $1",
            str(memory_id),
            vector_literal,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def two_users(db_pool, test_organization, clean_test_data):
    """Two ordinary users in the *same* organization.

    Same-org is deliberate: the org predicate in the search SQL cannot hide
    Bob's rows from Alice, so only the clearance scope can.
    """
    prefix = clean_test_data
    repo = UserRepository(db_pool)
    alice = await repo.create(
        external_id=f"{prefix}alice",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{prefix}alice@test.com",
        display_name=f"{prefix}Alice",
    )
    bob = await repo.create(
        external_id=f"{prefix}bob",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{prefix}bob@test.com",
        display_name=f"{prefix}Bob",
    )
    return alice, bob


@pytest_asyncio.fixture
async def daemon_user(db_pool, test_organization, clean_test_data):
    prefix = clean_test_data
    return await UserRepository(db_pool).create(
        external_id=f"{prefix}daemon",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{prefix}daemon@test.com",
        display_name=f"{prefix}Daemon",
        role="daemon",
    )


# ---------------------------------------------------------------------------
# SQL-shape tests: the vector expression lives inside the scoped statement
# ---------------------------------------------------------------------------


class _CaptureConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def execute(self, query: str, *params: object) -> str:
        self.queries.append(query)
        return "OK"

    async def fetchrow(self, query: str, *params: object):
        self.queries.append(query)
        return {"total": 0}

    async def fetch(self, query: str, *params: object):
        self.queries.append(query)
        return []


class _Acquire:
    def __init__(self, connection: _CaptureConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _CaptureConnection:
        return self._connection

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _InnerPool:
    def __init__(self, connection: _CaptureConnection) -> None:
        self._connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self._connection)


def _authorized_pool(connection: _CaptureConnection) -> AuthorizedDatabasePool:
    return AuthorizedDatabasePool(
        _InnerPool(connection),
        AuthPrincipal(uuid4(), "alice@test.com", uuid4()),
        (memory_db.MEMORY_AUTH_POLICY,),
    )


async def _fake_query_embedding(_query: str):
    return _literal(_unit(0)), MODEL_ID


def _assert_vector_sql_is_scoped(sql: str) -> None:
    """The guard the SQL-shape tests apply; also exercised by a meta-test.

    A statement that ranks by ``<=>`` must have had ``FROM memories``
    rewritten to the clearance-scoped subquery, or it can rank rows the caller
    may not see. ``) AS memories`` is the rewrite's outer alias; the
    ``auth_clearances`` lateral join is the actual predicate source.
    """
    assert "<=>" in sql, "test bug: no vector expression to check"
    assert "embedding_model = $" in sql, "vector arm must bind the model key"
    assert "auth_clearances" in sql, "vector ranking is not clearance-scoped"
    assert ") AS memories" in sql, "FROM memories was not rewritten to a scope"


def _disable_clearance_rewrite(monkeypatch) -> None:
    """Run the production search SQL *without* the clearance rewrite.

    Fail-before harness for the database isolation tests: the rewrite is the
    only thing separating them from the defect they guard. Patching the
    pool's single query-preparation hook disables it for every statement the
    repository emits (the ranking and its COUNT alike), so the same corpus is
    ranked globally instead of over the caller's clearances.
    """
    monkeypatch.setattr(
        AuthorizedDatabasePool, "_prepare_query", lambda self, query: query
    )


@pytest.mark.parametrize("method", ["search", "search_full"])
async def test_vector_expression_is_always_inside_clearance_scoped_sql(
    method, monkeypatch
) -> None:
    connection = _CaptureConnection()
    repo = MemoryRepository(_authorized_pool(connection))
    monkeypatch.setattr(memory_db, "_embedding_for_query", _fake_query_embedding)

    await getattr(repo, method)(
        query="rotate credentials",
        requesting_user_id=uuid4(),
        requesting_org_id=uuid4(),
        limit=5,
    )

    vector_statements = [query for query in connection.queries if "<=>" in query]
    assert vector_statements, "the vector arm never reached the database"
    for query in vector_statements:
        _assert_vector_sql_is_scoped(query)

    # The gate and the distance live in the same rewritten statement; the
    # scoped subquery alias is referenced exactly as a table would be. The
    # cosine floor is length-scaled (base for two-word queries, damped upward
    # as terms are added), so long many-concept queries cannot inflate
    # admission with a near-centroid embedding.
    assert all(
        ">= LEAST(" in query
        and str(MemoryRepository.VECTOR_MIN_SIMILARITY) in query
        for query in vector_statements
    )

    # The ranking statement (not the COUNT, which only re-uses the gate) is
    # the composite score over the same scoped rows.
    ranking_statements = [
        query
        for query in vector_statements
        if "ORDER BY sim_score DESC" in query or "ORDER BY final_rank DESC" in query
    ]
    assert ranking_statements
    assert all("0.70 *" in query for query in ranking_statements)


def test_scoped_vector_guard_rejects_a_hoisted_ranking() -> None:
    """Fail-before shape: the guard must reject the defect it exists for."""
    unscoped = """
        SELECT id, embedding <=> $1::vector AS distance
        FROM memories
        ORDER BY distance
        LIMIT 5
    """
    with pytest.raises(AssertionError):
        _assert_vector_sql_is_scoped(unscoped)


@pytest.mark.parametrize("method", ["search", "search_full"])
async def test_search_without_identity_emits_no_vector_arm(
    method, daemon_tenant_scope, monkeypatch
) -> None:
    """No resolvable scope => the pre-vector statement, even with an encoder.

    This is fail-closed, not widening: the semantic arm refuses to rank at all
    when there is no clearance-bound statement for it to run inside.
    """
    connection = _CaptureConnection()
    repo = MemoryRepository(_InnerPool(connection))
    # The encoder would happily return a vector; its absence from the SQL is
    # the scope gate, not an encoder failure.
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)
    monkeypatch.setattr(embeddings, "embed_text", lambda _text: _unit(0))

    await getattr(repo, method)(query="rotate credentials", limit=5)

    statements = [query for query in connection.queries if "ORDER BY" in query]
    assert statements
    for query in statements:
        assert "<=>" not in query
        assert "embedding_model" not in query


@pytest.mark.parametrize("method", ["search", "search_full"])
async def test_encoder_failure_sql_is_byte_identical_to_flag_off(
    method, monkeypatch
) -> None:
    """Graceful fallback must emit exactly the pre-vector expression."""
    async def _run(connection: _CaptureConnection) -> None:
        repo = MemoryRepository(_authorized_pool(connection))
        await getattr(repo, method)(
            query="rotate credentials",
            requesting_user_id=uuid4(),
            requesting_org_id=uuid4(),
            limit=5,
        )

    # Flag off: the untouched pre-change branch.
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: False)
    flag_off_connection = _CaptureConnection()
    await _run(flag_off_connection)
    flag_off = next(q for q in flag_off_connection.queries if "ORDER BY" in q)

    # Flag on, encoder raising: _embedding_for_query degrades to (None, None).
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)

    def _boom(_text: str) -> list[float] | None:
        raise RuntimeError("encoder offline")

    monkeypatch.setattr(embeddings, "embed_text", _boom)
    fallback_connection = _CaptureConnection()
    await _run(fallback_connection)
    fallback = next(q for q in fallback_connection.queries if "ORDER BY" in q)

    # The pre-change expression carries no vector references at all, and the
    # fallback reproduces it byte for byte.
    assert "embedding" not in flag_off
    assert "embedding" not in fallback
    assert "<=>" not in flag_off and "<=>" not in fallback
    assert fallback == flag_off


@pytest.mark.parametrize(
    "bad_vector",
    [
        [0.0] * (DIMENSIONS - 1),
        [float("nan")] * DIMENSIONS,
        [float("inf")] * DIMENSIONS,
    ],
    ids=["wrong-dimension", "nan", "inf"],
)
async def test_malformed_query_vector_degrades_to_lexical(
    bad_vector, monkeypatch, caplog
) -> None:
    """A bad query vector must read as "encoder unavailable for this call".

    Otherwise the ``$n::vector`` cast would raise against the ``vector(384)``
    column and fail the entire search — the opposite of the fail-open
    contract the write path already enforces.
    """
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)
    monkeypatch.setattr(embeddings, "embed_text", lambda _text: bad_vector)

    with caplog.at_level(logging.WARNING, logger="lucent.db.memory"):
        vector, model = await memory_db._embedding_for_query("rotate credentials")

    assert (vector, model) == (None, None)
    assert "memory_embedding_query_rejected" in caplog.text


# ---------------------------------------------------------------------------
# 1. Cross-user isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["search", "search_full"])
async def test_cross_user_vector_match_is_invisible_and_own_match_wins(
    method, db_pool, two_users, monkeypatch
) -> None:
    """Bob's row is a *better* vector match than Alice's, yet must not appear.

    Alice's row is at cosine 0.80, Bob's at 1.00. With ``limit=1`` an unscoped
    vector ranking would hand the slot to Bob; the clearance scope must hand
    it to Alice. Both are vector-only matches (no query term occurs in either
    row), so the lexical arm cannot be the thing that returns Alice.
    """
    alice, bob = two_users

    def resolver(text: str) -> list[float] | None:
        lowered = text.lower()
        if "forgetting mechanism" in lowered:  # the query
            return _unit(0)
        if "bob-private" in lowered:
            return _unit(0)
        if "alice-private" in lowered:
            return _cos_vector(0.80)
        return None

    _patch_encoder(monkeypatch, resolver)
    repo = MemoryRepository(db_pool)

    alice_memory = await _create(
        repo, alice, "alice-private: gradual memory decay pattern across sessions"
    )
    bob_memory = await _create(
        repo, bob, "bob-private: gradual memory decay pattern across sessions"
    )

    result = await getattr(repo, method)(
        query="forgetting mechanism",
        requesting_user_id=alice["id"],
        requesting_org_id=alice["organization_id"],
        limit=1,
    )

    returned_ids = [memory["id"] for memory in result["memories"]]
    assert returned_ids == [alice_memory["id"]]
    assert bob_memory["id"] not in returned_ids
    assert result["total_count"] == 1

    # Prove the exclusion is scope, not distance: Bob's row clears the 0.55
    # vector gate by a wider margin than Alice's.
    bob_similarity = await _embedding_similarity(
        db_pool, bob_memory["id"], _literal(_unit(0))
    )
    alice_similarity = await _embedding_similarity(
        db_pool, alice_memory["id"], _literal(_unit(0))
    )
    assert bob_similarity >= 0.999
    assert 0.79 <= alice_similarity <= 0.81


async def test_unscoped_vector_ranking_would_return_the_foreign_row(
    db_pool, two_users, monkeypatch
) -> None:
    """Witness, not a production contract: the same corpus ranked *without*
    the clearance rewrite gives the single slot to Bob.

    This proves the isolation test above is discriminating — the defect class
    it guards against (a vector ordering outside the scope) is expressible and
    would be observed, not assumed.
    """
    alice, bob = two_users

    def resolver(text: str) -> list[float] | None:
        lowered = text.lower()
        if "forgetting mechanism" in lowered:
            return _unit(0)
        if "bob-private" in lowered:
            return _unit(0)
        if "alice-private" in lowered:
            return _cos_vector(0.80)
        return None

    _patch_encoder(monkeypatch, resolver)
    repo = MemoryRepository(db_pool)
    await _create(repo, alice, "alice-private: gradual memory decay pattern")
    bob_memory = await _create(repo, bob, "bob-private: gradual memory decay pattern")

    async with db_pool.acquire() as conn:
        first = await conn.fetchval(
            """
            SELECT id FROM memories
            WHERE organization_id = $1 AND deleted_at IS NULL
              AND embedding IS NOT NULL AND embedding_model = $2
            ORDER BY embedding <=> $3::vector
            LIMIT 1
            """,
            str(alice["organization_id"]),
            MODEL_ID,
            _literal(_unit(0)),
        )

    assert first == bob_memory["id"]


async def test_disabling_the_clearance_rewrite_leaks_the_foreign_row(
    db_pool, two_users, monkeypatch
) -> None:
    """Fail-before on the *production* path: the isolation result above flips
    to Bob's row when the clearance rewrite is removed.

    The query, corpus, and `MemoryRepository.search()` call are identical to
    ``test_cross_user_vector_match_is_invisible_and_own_match_wins``; the only
    difference is that the rewrite is disabled. If that test passed for any
    reason other than scope (a lexical accident, a resolver quirk), this one
    would not return the foreign row.
    """
    alice, bob = two_users

    def resolver(text: str) -> list[float] | None:
        lowered = text.lower()
        if "forgetting mechanism" in lowered:
            return _unit(0)
        if "bob-private" in lowered:
            return _unit(0)
        if "alice-private" in lowered:
            return _cos_vector(0.80)
        return None

    _patch_encoder(monkeypatch, resolver)
    repo = MemoryRepository(db_pool)
    await _create(repo, alice, "alice-private: gradual memory decay pattern")
    bob_memory = await _create(repo, bob, "bob-private: gradual memory decay pattern")

    _disable_clearance_rewrite(monkeypatch)
    result = await repo.search(
        query="forgetting mechanism",
        requesting_user_id=alice["id"],
        requesting_org_id=alice["organization_id"],
        limit=1,
    )

    assert [memory["id"] for memory in result["memories"]] == [bob_memory["id"]]


async def test_org_shared_memory_is_visible_while_private_memory_is_not(
    db_pool, two_users, monkeypatch
) -> None:
    """Scoping is clearance-based: an explicit org read grant widens, ownership
    alone does not leak Bob's other rows to Alice."""
    alice, bob = two_users

    def resolver(text: str) -> list[float] | None:
        lowered = text.lower()
        if "forgetting mechanism" in lowered:
            return _unit(0)
        if "bob-" in lowered or "alice-" in lowered:
            return _unit(0)
        return None

    _patch_encoder(monkeypatch, resolver)
    repo = MemoryRepository(db_pool)

    alice_memory = await _create(
        repo, alice, "alice-private: gradual memory decay pattern"
    )
    bob_private = await _create(
        repo, bob, "bob-private: gradual memory decay pattern"
    )
    bob_shared = await _create(
        repo, bob, "bob-shared: gradual memory decay pattern", shared=True
    )

    result = await repo.search(
        query="forgetting mechanism",
        requesting_user_id=alice["id"],
        requesting_org_id=alice["organization_id"],
        limit=10,
    )

    returned_ids = {memory["id"] for memory in result["memories"]}
    assert alice_memory["id"] in returned_ids
    assert bob_shared["id"] in returned_ids
    assert bob_private["id"] not in returned_ids


async def test_org_shared_only_scope_sees_only_org_shared_rows(
    db_pool, two_users, daemon_user, monkeypatch
) -> None:
    """A daemon caller with ``memory_scope='org_shared_only'`` is confined to
    org read clearances: it sees the shared row, never a user's private row."""
    alice, bob = two_users

    def resolver(text: str) -> list[float] | None:
        lowered = text.lower()
        if "forgetting mechanism" in lowered:
            return _unit(0)
        if "bob-" in lowered or "alice-" in lowered:
            return _unit(0)
        return None

    _patch_encoder(monkeypatch, resolver)
    repo = MemoryRepository(db_pool)

    await _create(repo, alice, "alice-private: gradual memory decay pattern")
    bob_private = await _create(
        repo, bob, "bob-private: gradual memory decay pattern"
    )
    bob_shared = await _create(
        repo, bob, "bob-shared: gradual memory decay pattern", shared=True
    )

    result = await repo.search(
        query="forgetting mechanism",
        requesting_user_id=daemon_user["id"],
        requesting_org_id=daemon_user["organization_id"],
        memory_scope="org_shared_only",
        limit=10,
    )

    returned_ids = {memory["id"] for memory in result["memories"]}
    assert returned_ids == {bob_shared["id"]}
    assert bob_private["id"] not in returned_ids


async def test_search_without_org_scope_fails_closed(db_pool, two_users) -> None:
    """An identity without a resolvable org must raise, not widen."""
    alice, _bob = two_users
    repo = MemoryRepository(db_pool)

    with pytest.raises(TypeError, match="Memories usage reads require"):
        await repo.search(query="rotate credentials", requesting_user_id=alice["id"])


# ---------------------------------------------------------------------------
# 2. No unfiltered ranking: a foreign corpus cannot fill the LIMIT
# ---------------------------------------------------------------------------


async def test_foreign_rows_cannot_fill_the_limit(
    db_pool, two_users, monkeypatch
) -> None:
    """Alice has two memories, Bob has twenty better vector matches.

    A correctly scoped query returns two rows, fewer than the ``limit`` of
    five, even though foreign rows exist and clear the vector gate. If the
    ranking were global, those foreign rows would fill the window.
    """
    alice, bob = two_users

    def resolver(text: str) -> list[float] | None:
        lowered = text.lower()
        if "bob-vector" in lowered:
            return _unit(0)
        if lowered.strip() == "deployment checklist":  # the query only
            return _unit(0)
        return None

    _patch_encoder(monkeypatch, resolver)
    repo = MemoryRepository(db_pool)

    alice_one = await _create(
        repo, alice, "alice-limit: deployment checklist for the release"
    )
    alice_two = await _create(
        repo, alice, "alice-limit: deployment checklist for the rollback"
    )
    bob_memories = [
        await _create(repo, bob, f"bob-vector candidate {index}: unrelated prose")
        for index in range(20)
    ]

    result = await repo.search(
        query="deployment checklist",
        requesting_user_id=alice["id"],
        requesting_org_id=alice["organization_id"],
        limit=5,
    )

    returned_ids = [memory["id"] for memory in result["memories"]]
    assert set(returned_ids) == {alice_one["id"], alice_two["id"]}
    assert len(returned_ids) == 2 < 5
    assert result["total_count"] == 2

    # The foreign rows really do clear the vector gate; only scope hides them.
    bob_similarity = await _embedding_similarity(
        db_pool, bob_memories[0]["id"], _literal(_unit(0))
    )
    assert bob_similarity >= 0.999


async def test_disabling_the_clearance_rewrite_fills_the_limit_with_foreign_rows(
    db_pool, two_users, monkeypatch
) -> None:
    """Fail-before for the LIMIT claim: without the rewrite, Bob's foreign
    rows occupy the window Alice's query could not fill.

    Same corpus and call as ``test_foreign_rows_cannot_fill_the_limit``, with
    the rewrite disabled. Alice still has only two memories, so the five-slot
    window is provably filled by foreign rows — the observable signal the
    scoped test relies on.
    """
    alice, bob = two_users

    def resolver(text: str) -> list[float] | None:
        lowered = text.lower()
        if "bob-vector" in lowered:
            return _unit(0)
        if lowered.strip() == "deployment checklist":
            return _unit(0)
        return None

    _patch_encoder(monkeypatch, resolver)
    repo = MemoryRepository(db_pool)

    alice_ids = {
        (await _create(repo, alice, "alice-limit: deployment checklist for the release"))[
            "id"
        ],
        (await _create(repo, alice, "alice-limit: deployment checklist for the rollback"))[
            "id"
        ],
    }
    bob_ids = {
        (
            await _create(
                repo, bob, f"bob-vector candidate {index}: unrelated prose"
            )
        )["id"]
        for index in range(20)
    }

    _disable_clearance_rewrite(monkeypatch)
    result = await repo.search(
        query="deployment checklist",
        requesting_user_id=alice["id"],
        requesting_org_id=alice["organization_id"],
        limit=5,
    )

    returned_ids = {memory["id"] for memory in result["memories"]}
    assert len(returned_ids) == 5
    assert returned_ids - alice_ids, "foreign rows did not occupy the window"
    assert returned_ids & bob_ids, "the window was filled by something other than Bob"


# ---------------------------------------------------------------------------
# 3. Graceful fallback
# ---------------------------------------------------------------------------


async def test_encoder_failure_returns_lexical_results_and_no_error(
    db_pool, two_users, monkeypatch
) -> None:
    """Encoder unavailable => today's lexical result set, unchanged."""
    alice, _bob = two_users
    repo = MemoryRepository(db_pool)

    _patch_encoder(monkeypatch, lambda _text: _unit(0))
    memory = await _create(repo, alice, "deployment checklist for the release")

    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: False)
    baseline = await repo.search(
        query="deployment checklist",
        requesting_user_id=alice["id"],
        requesting_org_id=alice["organization_id"],
        limit=10,
    )
    assert [row["id"] for row in baseline["memories"]] == [memory["id"]]

    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)
    encoder_calls: list[str] = []

    def _boom(text: str) -> list[float] | None:
        encoder_calls.append(text)
        raise RuntimeError("encoder offline")

    monkeypatch.setattr(embeddings, "embed_text", _boom)

    fallback = await repo.search(
        query="deployment checklist",
        requesting_user_id=alice["id"],
        requesting_org_id=alice["organization_id"],
        limit=10,
    )

    assert encoder_calls, "the query encoder should have been attempted"
    assert [row["id"] for row in fallback["memories"]] == [
        row["id"] for row in baseline["memories"]
    ]
    assert [row["similarity_score"] for row in fallback["memories"]] == [
        row["similarity_score"] for row in baseline["memories"]
    ]


# ---------------------------------------------------------------------------
# 4. Model mismatch
# ---------------------------------------------------------------------------


async def test_stale_model_embedding_is_never_used_as_a_query_match(
    db_pool, two_users, monkeypatch
) -> None:
    """A stale-model vector is treated as unembedded, so it cannot be admitted
    by the vector gate even when it is identical to the query vector."""
    alice, _bob = two_users

    def resolver(text: str) -> list[float] | None:
        lowered = text.lower()
        if "forgetting mechanism" in lowered:
            return _unit(0)
        if "control-vector" in lowered or "stale-vector" in lowered:
            return _unit(0)
        return None

    _patch_encoder(monkeypatch, resolver)
    repo = MemoryRepository(db_pool)

    control = await _create(
        repo, alice, "control-vector: gradual memory decay pattern"
    )
    stale = await _create(repo, alice, "stale-vector: gradual memory decay pattern")

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET embedding_model = $2 WHERE id = $1",
            str(stale["id"]),
            STALE_MODEL_ID,
        )

    # Both vectors still equal the query vector; only the provenance differs.
    for memory in (control, stale):
        similarity = await _embedding_similarity(
            db_pool, memory["id"], _literal(_unit(0))
        )
        assert similarity >= 0.999

    result = await repo.search(
        query="forgetting mechanism",
        requesting_user_id=alice["id"],
        requesting_org_id=alice["organization_id"],
        limit=10,
    )

    returned_ids = {memory["id"] for memory in result["memories"]}
    assert control["id"] in returned_ids
    assert stale["id"] not in returned_ids


async def test_stale_model_row_scores_as_lexical(db_pool, two_users, monkeypatch) -> None:
    """With the vector arm on, a stale-model row's score must equal the score
    of an otherwise identical NULL-embedding row: the lexical fallback, not a
    composite that compared the stale vector."""
    alice, _bob = two_users

    def resolver(text: str) -> list[float] | None:
        lowered = text.lower()
        if "null-score" in lowered:
            return None  # this row must stay unembedded
        if "stale-score" in lowered:
            return _unit(0)
        if lowered.strip() == "deployment checklist":  # the query only
            return _unit(0)
        return None

    _patch_encoder(monkeypatch, resolver)
    repo = MemoryRepository(db_pool)

    lexical_content = "deployment checklist shared content for scoring"
    null_embedding = await _create(repo, alice, f"null-score {lexical_content}")
    stale = await _create(repo, alice, f"stale-score {lexical_content}")

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET embedding_model = $2 WHERE id = $1",
            str(stale["id"]),
            STALE_MODEL_ID,
        )

    result = await repo.search(
        query="deployment checklist",
        requesting_user_id=alice["id"],
        requesting_org_id=alice["organization_id"],
        limit=10,
    )

    scores = {memory["id"]: memory["similarity_score"] for memory in result["memories"]}
    assert null_embedding["id"] in scores and stale["id"] in scores
    # A stale vector compared to the current query would add 0.30 * similarity
    # to one row and not the other; equality is the observable contract.
    assert scores[stale["id"]] == pytest.approx(scores[null_embedding["id"]], abs=1e-9)


# ---------------------------------------------------------------------------
# 5. Exact-identifier priority with the vector arm enabled
# ---------------------------------------------------------------------------


async def test_exact_identifier_ranks_first_with_vector_arm_enabled(
    db_pool, two_users, monkeypatch
) -> None:
    """A lexical exact-identifier hit must beat similar-but-unrelated vectors.

    The exact row deliberately has no embedding, so nothing but the untouched
    lexical expression can lift it. The decoys clear the 0.55 vector gate.
    """
    alice, _bob = two_users
    identifier = "550e8400-e29b-41d4-a716-446655440000"
    path = "/Users/kyle/dev/lucent/daemon/worker.py:412"

    def resolver(text: str) -> list[float] | None:
        lowered = text.lower()
        if lowered.strip() == identifier:  # the query only
            return _unit(0)
        if "decoy-vector" in lowered:
            return _cos_vector(0.60)
        return None

    _patch_encoder(monkeypatch, resolver)
    repo = MemoryRepository(db_pool)

    exact = await _create(
        repo,
        alice,
        f"crash report {identifier} at {path}: NullReferenceError in dispatch",
    )
    decoys = [
        await _create(repo, alice, f"decoy-vector note {index}: operations prose")
        for index in range(8)
    ]

    # Confirm the exact row is genuinely unembedded (lexical-only lift).
    async with db_pool.acquire() as conn:
        exact_embedding = await conn.fetchval(
            "SELECT embedding::text FROM memories WHERE id = $1", str(exact["id"])
        )
    assert exact_embedding is None

    result = await repo.search(
        query=identifier,
        requesting_user_id=alice["id"],
        requesting_org_id=alice["organization_id"],
        limit=5,
    )

    returned_ids = [memory["id"] for memory in result["memories"]]
    assert returned_ids[0] == exact["id"]
    # The vector arm really ran: at least one decoy was admitted by it.
    assert set(returned_ids[1:]).intersection({decoy["id"] for decoy in decoys})


# ---------------------------------------------------------------------------
# 6. Paraphrase recall: on with the flag, demonstrably off without it
# ---------------------------------------------------------------------------


async def test_paraphrase_recall_on_but_not_off(
    db_pool, two_users, monkeypatch
) -> None:
    """'forgetting' vs 'decay': a lexical-only miss becomes a vector hit."""
    alice, _bob = two_users

    def resolver(text: str) -> list[float] | None:
        lowered = text.lower()
        if "forgetting" in lowered or "decay" in lowered:
            return _unit(0)
        return None

    _patch_encoder(monkeypatch, resolver)
    repo = MemoryRepository(db_pool)

    memory = await _create(
        repo, alice, "gradual memory decay is expected during long sessions"
    )
    assert "forgetting" not in memory["content"].lower()

    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: False)
    off = await repo.search(
        query="forgetting",
        requesting_user_id=alice["id"],
        requesting_org_id=alice["organization_id"],
        limit=10,
    )
    assert off["total_count"] == 0
    assert memory["id"] not in {row["id"] for row in off["memories"]}

    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)
    on = await repo.search(
        query="forgetting",
        requesting_user_id=alice["id"],
        requesting_org_id=alice["organization_id"],
        limit=10,
    )
    assert memory["id"] in {row["id"] for row in on["memories"]}


# ---------------------------------------------------------------------------
# 7. Write-path fail-open
# ---------------------------------------------------------------------------


async def test_write_path_fail_open_persists_null_embedding_and_warns(
    db_pool, two_users, monkeypatch, caplog
) -> None:
    alice, _bob = two_users
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)

    def _boom(_text: str) -> list[float] | None:
        raise RuntimeError("encoder exploded")

    monkeypatch.setattr(embeddings, "embed_text", _boom)
    repo = MemoryRepository(db_pool)

    with caplog.at_level(logging.WARNING, logger="lucent.db.memory"):
        memory = await _create(repo, alice, "fail-open write path content")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT embedding::text AS embedding_text, embedding_model, embedded_at
               FROM memories WHERE id = $1""",
            str(memory["id"]),
        )
    assert row["embedding_text"] is None
    assert row["embedding_model"] is None
    assert row["embedded_at"] is None

    assert "memory_embedding_write_failed" in caplog.text
    assert any(
        getattr(record, "error", None) == "encoder exploded"
        for record in caplog.records
        if record.name == "lucent.db.memory"
    )

    stored = await repo.get(memory["id"], organization_id=alice["organization_id"])
    assert stored is not None
    assert stored["content"] == "fail-open write path content"
