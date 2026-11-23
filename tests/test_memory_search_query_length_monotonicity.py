"""Monotonicity and precision tests for long-query memory search.

Contract under test
-------------------
Selectivity is monotonically non-increasing as query terms are appended::

    returned_count(prefix) >= returned_count(prefix + extra terms)

for every prefix of the ladder, with the semantic arm both off and on. The
lexical gate makes this provable by construction: a row must contain every term
of the query's anchored core (its first ``TERM_COVERAGE_CORE_TERMS`` content
terms), and a longer query's core is a superset of every shorter prefix's core.
The semantic arm is held to the same shape by a length-scaled cosine floor, so a
many-concept query's near-centroid embedding cannot out-admit a specific short
query.

The seeded corpus
-----------------
``SEEDED_CORPUS`` below is seven experience memories in one test organization,
each named for the property it exercises. It is reproducible: run

    pytest -m integration tests/test_memory_search_query_length_monotonicity.py

against the disposable ``lucent-test-db`` (``scripts/dev-test-db.sh``). The
encoder is faked, so no model is downloaded. The ladder queries are
``LEADING_CORE_LADDER``; every one starts with "memory search" and the fake
encoder deliberately maps all of them to the *same* centroid vector — the worst
case for embedding drift — so the only thing that can keep long queries narrow
is the damped floor.
"""

from __future__ import annotations

import pytest

from lucent.db import MemoryRepository
from lucent.db import memory as memory_db
from lucent.memory import embeddings

DIMENSIONS = embeddings.EMBEDDING_DIMENSIONS

# label -> content. Every row is deliberately short so stemming and the
# relevance floor cannot mask the coverage rule being measured.
# Every content string carries a ``seed:`` prefix so it can never be identical
# to a ladder query: the fake encoder keys query vectors off the exact query
# text, and a content row equal to a query string would otherwise be embedded
# as an exact (cosine 1.0) vector hit and bypass the lexical gate.
SEEDED_CORPUS: dict[str, str] = {
    # Holds the whole four-term core (memory, search, relevance, scoring).
    "all-four": "seed: memory search relevance scoring",
    # Each shorter core prefix.
    "first-three": "seed: memory search relevance",
    "first-two": "seed: memory search",
    # Whole core plus the extra prose terms the long query adds.
    "all-four-plus-prose": (
        "seed: memory search relevance scoring rank results long natural language queries"
    ),
    # Only the prose terms, none of the core: the long query must NOT admit it.
    "extra-terms-only-decoy": (
        "seed: relevance scoring rank results long natural language queries"
    ),
    # Two of the core terms only: admitted by "memory search", not by
    # "memory search relevance".
    "two-of-core-decoy": "seed: memory search vector encoder deployment",
    "unrelated": "seed: vector encoder deployment rollback procedure",
    # No query terms at all. Admitted only by the semantic arm, which is
    # exactly what the length-scaled floor must damp out of long queries.
    "vector-only-decoy": "decoy-vector note: gradual memory decay pattern",
}

# Each entry appends terms to the previous one, so every adjacent pair is a
# prefix/extension pair for the monotonicity assertion.
LEADING_CORE_LADDER: list[str] = [
    "memory search",
    "memory search relevance",
    "memory search relevance scoring",
    "memory search relevance scoring rank results",
    "memory search relevance scoring rank results long natural language queries",
]


# ---------------------------------------------------------------------------
# Deterministic vectors + faked encoder
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


def _patch_encoder(monkeypatch, resolver) -> list[str]:
    """Force the vector flag on and route every embed through ``resolver``.

    Returns the texts handed to the encoder so a test can prove the arm ran.
    """
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: True)
    calls: list[str] = []

    def _embed(text: str) -> list[float] | None:
        calls.append(text)
        return resolver(text)

    monkeypatch.setattr(embeddings, "embed_text", _embed)
    return calls


def _centroid_resolver(text: str) -> list[float] | None:
    """One shared centroid for every ladder query; cosine 0.60 for the decoy."""
    lowered = text.lower()
    if lowered.startswith("memory search") or lowered.strip() == "forgetting mechanism":
        return _unit(0)
    if "decoy-vector" in lowered:
        return _cos_vector(0.60)
    return None


async def _seed_corpus(repo: MemoryRepository, user: dict) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for label, content in SEEDED_CORPUS.items():
        rows[label] = await repo.create(
            username=None,
            type="experience",
            content=content,
            tags=["query-length"],
            importance=5,
            user_id=user["id"],
            organization_id=user["organization_id"],
        )
    return rows


async def _search(repo: MemoryRepository, user: dict, query: str) -> dict:
    return await repo.search(
        query=query,
        requesting_user_id=user["id"],
        requesting_org_id=user["organization_id"],
        limit=50,
    )


def _ids(result: dict) -> set:
    return {memory["id"] for memory in result["memories"]}


# ---------------------------------------------------------------------------
# Monotonicity: lexical arm alone (provable by construction)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["search", "search_full"])
async def test_returned_count_is_monotone_across_the_seeded_ladder(
    method, db_pool, test_user, monkeypatch
) -> None:
    """count(prefix) >= count(prefix + extra), and sets nest, arm off."""
    monkeypatch.setattr(memory_db, "search_vector_enabled", lambda: False)
    repo = MemoryRepository(db_pool)
    rows = await _seed_corpus(repo, test_user)

    counts: list[int] = []
    result_sets: list[set] = []
    for query in LEADING_CORE_LADDER:
        result = await getattr(repo, method)(
            query=query,
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
            limit=50,
        )
        counts.append(result["total_count"])
        result_sets.append(_ids(result))

    assert counts == sorted(counts, reverse=True), dict(
        zip(LEADING_CORE_LADDER, counts)
    )
    for shorter, longer in zip(result_sets, result_sets[1:]):
        assert longer <= shorter, f"adding terms widened the result set: {longer - shorter}"

    longest = result_sets[-1]
    # Extra prose must not admit a row that lacks every core term.
    assert rows["extra-terms-only-decoy"]["id"] not in longest
    assert rows["unrelated"]["id"] not in longest
    # The row holding the whole core survives.
    assert rows["all-four"]["id"] in longest
    assert rows["all-four-plus-prose"]["id"] in longest


# ---------------------------------------------------------------------------
# Monotonicity: semantic arm enabled (floor damped by word count)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["search", "search_full"])
async def test_returned_count_is_monotone_with_the_vector_arm_enabled(
    method, db_pool, test_user, monkeypatch
) -> None:
    """The damped floor keeps the semantic arm from breaking the ladder.

    The vector-only decoy sits at cosine 0.60. It is admitted by the two-word
    query (floor 0.55) and the three-word query (floor 0.58), and dropped from
    the four-word query onward (floor 0.61+). A fixed 0.55 floor would keep
    admitting it for every longer query.
    """
    calls = _patch_encoder(monkeypatch, _centroid_resolver)
    repo = MemoryRepository(db_pool)
    rows = await _seed_corpus(repo, test_user)

    counts: list[int] = []
    result_sets: list[set] = []
    for query in LEADING_CORE_LADDER:
        result = await getattr(repo, method)(
            query=query,
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
            limit=50,
        )
        counts.append(result["total_count"])
        result_sets.append(_ids(result))

    assert calls, "the query encoder never ran — the vector arm was not exercised"
    assert counts == sorted(counts, reverse=True), dict(
        zip(LEADING_CORE_LADDER, counts)
    )
    for shorter, longer in zip(result_sets, result_sets[1:]):
        assert longer <= shorter, f"adding terms widened the result set: {longer - shorter}"

    decoy = rows["vector-only-decoy"]["id"]
    assert decoy in result_sets[0], "short query should admit the 0.60 decoy"
    assert decoy not in result_sets[-1], "long query must damp the 0.60 decoy"


# ---------------------------------------------------------------------------
# Precision: a 6-10 word question vs its two-term core
# ---------------------------------------------------------------------------


async def test_long_natural_language_query_is_no_broader_than_its_two_term_core(
    db_pool, test_user, monkeypatch
) -> None:
    """The 10-word question returns no more rows than "memory search"."""
    _patch_encoder(monkeypatch, _centroid_resolver)
    repo = MemoryRepository(db_pool)
    rows = await _seed_corpus(repo, test_user)

    core_query = LEADING_CORE_LADDER[0]
    long_query = LEADING_CORE_LADDER[-1]
    assert len(long_query.split()) >= 6  # the shape the defect was reported for

    core = await _search(repo, test_user, core_query)
    long = await _search(repo, test_user, long_query)

    core_ids = _ids(core)
    long_ids = _ids(long)
    assert long["total_count"] <= core["total_count"], (
        long["total_count"],
        core["total_count"],
    )
    assert long_ids <= core_ids

    # Extra words must not admit semantically adjacent but core-less rows.
    assert rows["extra-terms-only-decoy"]["id"] not in long_ids
    assert rows["unrelated"]["id"] not in long_ids
    assert rows["vector-only-decoy"]["id"] not in long_ids

    # Every surviving row really holds the whole core.
    contents = {memory["id"]: memory["content"].lower() for memory in long["memories"]}
    for memory_id in long_ids:
        content = contents[memory_id]
        assert "memory" in content
        assert "search" in content
        assert "relevance" in content
        assert "scoring" in content


# ---------------------------------------------------------------------------
# Damping in isolation: a many-concept query cannot inflate admission
# ---------------------------------------------------------------------------


async def test_vector_arm_is_damped_for_many_concept_queries(
    db_pool, test_user, monkeypatch
) -> None:
    """Six unrelated rows at cosine 0.60: admitted short, excluded long."""
    _patch_encoder(monkeypatch, _centroid_resolver)
    repo = MemoryRepository(db_pool)

    decoys = [
        await repo.create(
            username=None,
            type="experience",
            content=f"decoy-vector note {index}: unrelated operational prose",
            tags=["query-length"],
            importance=5,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        for index in range(6)
    ]

    short = await _search(repo, test_user, "forgetting mechanism")  # 2 words
    long = await _search(repo, test_user, LEADING_CORE_LADDER[-1])  # 10 words

    assert short["total_count"] == len(decoys)
    assert _ids(short) == {decoy["id"] for decoy in decoys}
    assert long["total_count"] == 0, (
        "a 10-word query admitted vector-only rows at the short-query floor"
    )
    # The empty result explains itself rather than reading as "no such memory".
    assert long["match_quality"]["reason"] in {
        "no_memory_contains_any_query_term",
        "query_vocabulary_not_in_corpus",
        "terms_too_scattered",
    }


# ---------------------------------------------------------------------------
# Exact identifiers keep the base floor (damping is keyed on word count)
# ---------------------------------------------------------------------------


async def test_exact_identifier_query_keeps_the_base_vector_floor(
    db_pool, test_user, monkeypatch
) -> None:
    """A UUID is one word but many lexemes; it must not be damped."""
    identifier = "550e8400-e29b-41d4-a716-446655440000"

    def resolver(text: str) -> list[float] | None:
        lowered = text.lower()
        if lowered.strip() == identifier:
            return _unit(0)
        if "decoy-vector" in lowered:
            return _cos_vector(0.60)
        return None

    _patch_encoder(monkeypatch, resolver)
    repo = MemoryRepository(db_pool)

    exact = await repo.create(
        username=None,
        type="experience",
        content=f"crash report {identifier}: dispatch failure in worker",
        tags=["query-length", "identifier"],
        importance=5,
        user_id=test_user["id"],
        organization_id=test_user["organization_id"],
    )
    decoys = [
        await repo.create(
            username=None,
            type="experience",
            content=f"decoy-vector note {index}: unrelated operational prose",
            tags=["query-length"],
            importance=5,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        for index in range(6)
    ]

    result = await _search(repo, test_user, identifier)
    returned_ids = [memory["id"] for memory in result["memories"]]

    assert returned_ids[0] == exact["id"], "exact identifier must rank first"
    # The vector arm still ran at the base 0.55 floor for this one-word query.
    assert set(returned_ids[1:]) & {decoy["id"] for decoy in decoys}
