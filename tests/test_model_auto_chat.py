"""Auto model selection: chat integration (Phase 4).

Exercises chat's real ``_auto_chat_model_choice`` against the test database —
seeding enabled, granted models (plus a decision-class row) and monkeypatching
only the arbiter's decision call. Covers candidate filtering, vision-fit
triggering from image attachments, the arbiter's usage ledger row, and the
create-session sentinel authoring gate.
"""

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException

from lucent.api.routers import chat
from lucent.db.runtime_settings import RuntimeSettingsRepository
from lucent.llm.decision import ChoiceAnswer, DecisionResponse, DecisionUsage
from lucent.settings import clear_runtime_setting_cache, set_runtime_setting_cache

FLAG_KEY = "models.auto_selection_enabled"
ARB_ID = "22222222-aaaa-bbbb-cccc-222222222222"


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_seeded_orgs(request, cleanup_test_organizations):
    """Delete the organizations this suite seeds inline, after each test.

    ``_seed_chat_org`` builds org/user rows by hand (no per-test prefix
    fixture); the tests only undo their models and flags, so without this the
    orgs outlive the test and pile up in the shared test database. Gated on the
    integration marker so the module's pure unit tests never open a connection.
    """
    is_integration = request.node.get_closest_marker("integration") is not None
    yield
    if is_integration:
        await cleanup_test_organizations(["autoc_%"])


async def _seed_chat_org(db_pool, prefix: str):
    from lucent.db import OrganizationRepository, UserRepository

    org = await OrganizationRepository(db_pool).create(name=f"{prefix}org")
    user = await UserRepository(db_pool).create(
        external_id=f"{prefix}u",
        provider="basic",
        organization_id=org["id"],
        email=f"{prefix}@test.com",
        display_name="Auto Chat User",
    )
    return org, user


async def _create_granted_model(
    db_pool,
    org,
    user,
    model_id: str,
    *,
    supports_vision: bool = False,
    model_class: str = "language",
    context_window: int = 1_000_000,
    is_enabled: bool = True,
) -> None:
    from lucent.db.models import ModelRepository

    await ModelRepository(db_pool).create_model(
        model_id=model_id,
        provider="ollama",
        name=model_id.replace("-", " ").title(),
        category="general",
        model_class=model_class,
        context_window=context_window,
        supports_vision=supports_vision,
        engine="langchain",
        is_enabled=is_enabled,
        org_id=str(org["id"]),
    )
    # File the org-read clearance the models grant UI writes (default deny).
    await db_pool.execute(
        """
        INSERT INTO auth_clearances
            (auth_id, role, principal_type, principal_id, granted_by)
        SELECT m.auth_id, 'read', 'org', m.organization_id, $2::uuid
        FROM models m WHERE m.id = $1
        ON CONFLICT DO NOTHING
        """,
        model_id,
        UUID(str(user["id"])),
    )


async def _cleanup_org_models(db_pool, org) -> None:
    await db_pool.execute(
        """
        DELETE FROM auth_clearances
        WHERE auth_id IN (SELECT auth_id FROM models WHERE organization_id = $1)
        """,
        UUID(str(org["id"])),
    )
    await db_pool.execute(
        "DELETE FROM models WHERE organization_id = $1", UUID(str(org["id"]))
    )


async def _set_flag(db_pool, org, user, on: bool) -> None:
    await RuntimeSettingsRepository(db_pool).upsert_setting(
        organization_id=str(org["id"]),
        key=FLAG_KEY,
        value=on,
        value_type="boolean",
        user_id=str(user["id"]),
    )
    set_runtime_setting_cache(str(org["id"]), FLAG_KEY, on)


async def _clear_flag(db_pool, org) -> None:
    await RuntimeSettingsRepository(db_pool).delete_setting(
        str(org["id"]), FLAG_KEY
    )
    clear_runtime_setting_cache(str(org["id"]), FLAG_KEY)


def _user_dict(org, user) -> dict:
    return {"id": str(user["id"]), "organization_id": str(org["id"])}


async def _resolve_arbiter(_explicit, _org):
    return ARB_ID


def _canned_decide(choice_id: str, candidates: list[str], captured: list):
    async def _decide(request, **_kw):
        captured.append(request)
        return DecisionResponse(
            model="nimble",
            answers={
                "best_model": ChoiceAnswer(
                    choice=choice_id,
                    probabilities={
                        **{mid: 0.1 for mid in candidates if mid != choice_id},
                        choice_id: 0.85,
                    },
                    confidence=0.9,
                )
            },
            usage=DecisionUsage(input_tokens=110, output_tokens=21),
        )

    return _decide


# ── flag gating ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_chat_choice_flag_off_is_a_400(db_pool, monkeypatch):
    org, user = await _seed_chat_org(db_pool, f"autoc_off_{str(uuid4())[:8]}")

    with pytest.raises(HTTPException) as exc:
        await chat._auto_chat_model_choice(
            user=_user_dict(org, user),
            pool=db_pool,
            messages=[chat.ChatMessage(role="user", content="hello")],
            session_id=None,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "Auto model selection is disabled"


# ── candidate pool + arbiter ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_chat_choice_consults_arbiter_and_records_usage(
    db_pool, monkeypatch
):
    org, user = await _seed_chat_org(db_pool, f"autoc_pick_{str(uuid4())[:8]}")
    suffix = str(org["id"])[:8]
    chat_a, chat_b = f"chat-a-{suffix}", f"chat-b-{suffix}"
    await _create_granted_model(db_pool, org, user, chat_a)
    await _create_granted_model(db_pool, org, user, chat_b)
    # Decision-class rows exist but never enter the candidate pool.
    await _create_granted_model(db_pool, org, user, f"dec-x-{suffix}", model_class="decision")
    captured: list = []
    monkeypatch.setattr(
        "lucent.llm.model_auto.decide", _canned_decide(chat_a, [chat_a, chat_b], captured)
    )
    monkeypatch.setattr("lucent.llm.model_auto._resolve_arbiter_model", _resolve_arbiter)
    try:
        await _set_flag(db_pool, org, user, True)
        choice = await chat._auto_chat_model_choice(
            user=_user_dict(org, user),
            pool=db_pool,
            messages=[chat.ChatMessage(role="user", content="Summarize notes")],
            session_id=None,
        )

        assert choice.source == "arbiter"
        assert choice.model_id == chat_a
        assert choice.usage == (110, 21)
        criteria = captured[0].questions["best_model"].criteria
        assert set(criteria) == {chat_a, chat_b}

        row = await db_pool.fetchrow(
            """
            SELECT model, engine, input_tokens, output_tokens
            FROM llm_token_usage
            WHERE organization_id = $1 AND engine = 'decision'
            ORDER BY created_at DESC LIMIT 1
            """,
            UUID(str(org["id"])),
        )
        assert row is not None
        assert row["model"] == ARB_ID
        assert row["input_tokens"] == 110
    finally:
        await _clear_flag(db_pool, org)
        await _cleanup_org_models(db_pool, org)


@pytest.mark.asyncio
async def test_auto_chat_choice_image_attachment_requires_vision(
    db_pool, monkeypatch
):
    org, user = await _seed_chat_org(db_pool, f"autoc_vis_{str(uuid4())[:8]}")
    suffix = str(org["id"])[:8]
    chat_v, chat_nv = f"chat-v-{suffix}", f"chat-nv-{suffix}"
    await _create_granted_model(
        db_pool, org, user, chat_v, supports_vision=True, context_window=100_000
    )
    await _create_granted_model(
        db_pool, org, user, chat_nv, supports_vision=False, context_window=1_000_000
    )

    async def _never(_request, **_kw):
        raise AssertionError("single survivor must skip the arbiter call")

    monkeypatch.setattr("lucent.llm.model_auto.decide", _never)
    monkeypatch.setattr("lucent.llm.model_auto._resolve_arbiter_model", _resolve_arbiter)
    try:
        await _set_flag(db_pool, org, user, True)
        choice = await chat._auto_chat_model_choice(
            user=_user_dict(org, user),
            pool=db_pool,
            messages=[
                chat.ChatMessage(
                    role="user",
                    content="What is in this diagram?",
                    attachments=[
                        chat.ChatAttachment(
                            kind="image", mime_type="image/png", data="aGk="
                        )
                    ],
                )
            ],
            session_id=None,
        )

        assert choice.source == "arbiter"
        assert choice.model_id == chat_v
        assert (chat_nv, "does not support image input") in choice.dropped
    finally:
        await _clear_flag(db_pool, org)
        await _cleanup_org_models(db_pool, org)


# ── create-session sentinel gate ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_chat_session_sentinel_gate(db_pool, monkeypatch):

    org, user = await _seed_chat_org(db_pool, f"autoc_sess_{str(uuid4())[:8]}")

    async def _fake_session_user(_request):
        return {"id": str(user["id"]), "organization_id": str(org["id"])}, db_pool

    monkeypatch.setattr(chat, "_get_session_user", _fake_session_user)

    # Flag off — refused with the same authoring message as tasks.
    with pytest.raises(HTTPException) as exc:
        await chat.create_chat_session(
            object(), chat.CreateChatSession(title="auto chat", model="auto")
        )
    assert exc.value.status_code == 422
    assert exc.value.detail == "Auto model selection is disabled"

    # Flag on — stored verbatim; the stream resolves it per message.
    await _set_flag(db_pool, org, user, True)
    try:
        session = await chat.create_chat_session(
            object(), chat.CreateChatSession(title="auto chat", model="auto")
        )
        assert session["model"] == "auto"
    finally:
        await _clear_flag(db_pool, org)
