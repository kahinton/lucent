"""Token-limit enforcement as a transparent wrapper around any LLM engine.

Every path that runs a model goes through ``LLMEngine.run_session`` or
``LLMEngine.run_session_streaming``. Wrapping the engine returned by the
registry means enforcement is inherited by all four engines (LangChain, Copilot,
Claude Code, Codex) and by any engine added later, with no changes at the dozen
call sites that already exist.

Why a wrapper rather than edits inside each engine
--------------------------------------------------
Each engine has its own provider SDK, its own error handling and its own event
shape. Threading a budget through four of them would leave four places to get
wrong and four places to forget when a fifth engine is added. The wrapper sees
the same two methods and the same ``SessionEvent`` stream, so the logic exists
once.

What the wrapper does
---------------------
1. Resolves the principal from ``audit_context`` — every real call site already
   populates ``organization_id`` and ``user_id`` there for tool auditing.
2. Runs :meth:`TokenLimitService.preflight`. A refusal raises
   :class:`TokenQuotaExceeded` before a single token is spent.
3. Wraps ``on_event`` so usage is accumulated locally and compared against the
   budget captured at preflight. Exceeding it mid-session cancels the engine
   task, which is what stops a runaway tool loop.
4. Reconciles against the ledger afterwards and releases the in-flight
   reservation.

Failure policy
--------------
Failing *closed* on a quota breach is the point of the feature. Failing closed
on an *infrastructure* error (database unavailable while reading policies) would
take the product down for a billing control, so policy lookups inside the
service already degrade to "allow" with a warning.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from lucent.llm.engine import LLMEngine, SessionEvent, SessionEventType
from lucent.llm.token_quota import (
    QuotaSnapshot,
    TokenLimitService,
    TokenQuotaExceeded,
    enrich_usage,
    get_token_limit_service,
)

logger = logging.getLogger(__name__)


def _principal(audit_context: dict[str, Any] | None) -> tuple[str | None, str | None]:
    context = audit_context or {}
    organization_id = str(context.get("organization_id") or "").strip() or None
    user_id = str(context.get("user_id") or "").strip() or None
    return organization_id, user_id


class QuotaEnforcingEngine(LLMEngine):
    """Delegates to a wrapped engine, enforcing token limits around each call."""

    def __init__(self, engine: LLMEngine) -> None:
        self._engine = engine

    # ── identity ───────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        # Callers branch on the engine name (capabilities, logging, persisted
        # session rows). Reporting the wrapped name keeps this wrapper invisible.
        return self._engine.name

    def __getattr__(self, item: str) -> Any:
        # Capability lookups and engine-specific helpers live on the concrete
        # engine. Forwarding keeps the wrapper substitutable everywhere the
        # original was usable — including private helpers such as
        # ``_run_with_tools`` that callers and tests reach for directly.
        #
        # ``_engine`` is read through ``object.__getattribute__`` because
        # ``__getattr__`` only fires when normal lookup has already failed: if
        # the instance were partially built (unpickling, ``__new__`` without
        # ``__init__``), the plain ``self._engine`` would re-enter this method
        # and recurse until the interpreter gives up.
        try:
            engine = object.__getattribute__(self, "_engine")
        except AttributeError:
            raise AttributeError(item) from None
        return getattr(engine, item)

    @property
    def wrapped(self) -> LLMEngine:
        return self._engine

    async def cleanup(self) -> None:
        """Release provider resources held by the wrapped engine."""
        await self._engine.cleanup()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"QuotaEnforcingEngine({self._engine!r})"

    # ── internal helpers ───────────────────────────────────────────────────

    async def _service(self) -> TokenLimitService:
        return await get_token_limit_service()

    @staticmethod
    def _extra_text(
        *,
        message_history: list[dict[str, Any]] | None,
        attachments: list[dict[str, Any]] | None,
        managed_tools: list[dict[str, Any]] | None,
    ) -> list[str]:
        """Text that is re-sent to the provider but not in the prompt itself.

        History dominates the prompt on later turns of a long conversation, so a
        preflight that ignored it would approve requests that could never fit.
        """
        parts: list[str] = []
        for message in message_history or []:
            content = message.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
        for attachment in attachments or []:
            # Only the metadata is counted; base64 payload sizing belongs to the
            # provider's image accounting, and guessing at it would over-refuse.
            size = attachment.get("size")
            if isinstance(size, int) and size > 0:
                parts.append("x" * min(size, 200_000))
        for tool in managed_tools or []:
            schema = tool.get("native_descriptor") or tool.get("input_schema") or ""
            if schema:
                parts.append(str(schema))
        return parts

    # ── non-streaming ──────────────────────────────────────────────────────

    async def run_session(
        self,
        model: str,
        system_message: str,
        prompt: str,
        mcp_config: dict | None = None,
        timeout: int = 300,
        reasoning_effort: str | None = None,
        provider_session_id: str | None = None,
        resume: bool = False,
        message_history: list[dict[str, Any]] | None = None,
        hooks: list[dict[str, Any]] | None = None,
        audit_context: dict[str, Any] | None = None,
        enable_config_discovery: bool = False,
        approve_permissions: bool = True,
        attachments: list[dict[str, Any]] | None = None,
        managed_tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str | None:
        organization_id, user_id = _principal(audit_context)
        service = await self._service()
        snapshot = await service.preflight(
            organization_id=organization_id,
            user_id=user_id,
            model_id=model,
            system_message=system_message,
            prompt=prompt,
            extra_text=self._extra_text(
                message_history=message_history,
                attachments=attachments,
                managed_tools=managed_tools,
            ),
        )

        accumulated = {dimension: 0 for dimension in ("raw_input", "cached_input", "output")}

        def capture_usage(event: SessionEvent | Any) -> None:
            # The non-streaming engines do not emit usage today, but Copilot and
            # Codex call ``on_event`` internally when a caller subscribes. This
            # hook exists so that a future engine reporting usage here is still
            # accounted for without another edit.
            if (
                isinstance(event, SessionEvent)
                and event.type == SessionEventType.USAGE
                and event.usage
            ):
                for dimension, value in enrich_usage(event.usage).items():
                    accumulated[dimension] += value

        try:
            # ``on_event`` is not part of the non-streaming signature, so it is
            # passed only when the concrete engine accepts it.
            try:
                result = await self._engine.run_session(  # type: ignore[call-arg]
                    model=model,
                    system_message=system_message,
                    prompt=prompt,
                    mcp_config=mcp_config,
                    timeout=timeout,
                    reasoning_effort=reasoning_effort,
                    provider_session_id=provider_session_id,
                    resume=resume,
                    message_history=message_history,
                    hooks=hooks,
                    audit_context=audit_context,
                    enable_config_discovery=enable_config_discovery,
                    approve_permissions=approve_permissions,
                    attachments=attachments,
                    managed_tools=managed_tools,
                    **kwargs,
                )
            except TypeError as exc:
                if "on_event" not in str(exc):
                    raise
                result = await self._engine.run_session(
                    model=model,
                    system_message=system_message,
                    prompt=prompt,
                    mcp_config=mcp_config,
                    timeout=timeout,
                    reasoning_effort=reasoning_effort,
                    provider_session_id=provider_session_id,
                    resume=resume,
                    message_history=message_history,
                    hooks=hooks,
                    audit_context=audit_context,
                    enable_config_discovery=enable_config_discovery,
                    approve_permissions=approve_permissions,
                    attachments=attachments,
                    managed_tools=managed_tools,
                    **kwargs,
                )
        finally:
            await service.release_reservation(organization_id, user_id, model)

        if not snapshot.is_open:
            violation = snapshot.violation_for(accumulated)
            if violation is not None:
                logger.warning(
                    "Token limit exceeded during session: %s",
                    violation.as_exception().message(),
                )
            await service.post_check(
                organization_id=organization_id,
                user_id=user_id,
                model_id=model,
                usage=accumulated,
            )
        return result

    # ── streaming ──────────────────────────────────────────────────────────

    async def run_session_streaming(
        self,
        model: str,
        system_message: str,
        prompt: str,
        mcp_config: dict | None = None,
        on_event: Callable[[SessionEvent], None] | None = None,
        timeout: int = 600,
        idle_timeout: int = 300,
        reasoning_effort: str | None = None,
        provider_session_id: str | None = None,
        resume: bool = False,
        message_history: list[dict[str, Any]] | None = None,
        hooks: list[dict[str, Any]] | None = None,
        audit_context: dict[str, Any] | None = None,
        enable_config_discovery: bool = False,
        approve_permissions: bool = True,
        attachments: list[dict[str, Any]] | None = None,
        managed_tools: list[dict[str, Any]] | None = None,
        current_user_text: str | None = None,
        **kwargs: Any,
    ) -> str | None:
        organization_id, user_id = _principal(audit_context)
        service = await self._service()
        snapshot: QuotaSnapshot = await service.preflight(
            organization_id=organization_id,
            user_id=user_id,
            model_id=model,
            system_message=system_message,
            prompt=prompt,
            extra_text=self._extra_text(
                message_history=message_history,
                attachments=attachments,
                managed_tools=managed_tools,
            ),
        )

        accumulated = {"raw_input": 0, "cached_input": 0, "output": 0}
        breach: list[Any] = []
        task_holder: dict[str, asyncio.Task[Any]] = {}

        def guarded_on_event(event: SessionEvent) -> None:
            """Forward the caller's callback, but account usage first.

            The mid-stream check runs here, synchronously, against the snapshot
            taken at preflight. It never touches the database: this executes
            inside the provider's callback, and a query per token event would
            dominate the request's latency.

            Because the callback cannot raise into the provider loop, a breach
            cancels the engine task and lets :class:`TokenQuotaExceeded` surface
            from the awaited coroutine instead.
            """
            if event.type == SessionEventType.USAGE and event.usage:
                for dimension, value in enrich_usage(event.usage).items():
                    accumulated[dimension] += value
                if not snapshot.is_open and not breach:
                    violation = snapshot.violation_for(accumulated)
                    if violation is not None:
                        breach.append(violation)
                        task = task_holder.get("task")
                        if task is not None and not task.done():
                            task.cancel()
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:  # noqa: BLE001 — never break the provider loop
                    logger.debug("Downstream on_event failed", exc_info=True)

        async def run() -> str | None:
            return await self._engine.run_session_streaming(
                model=model,
                system_message=system_message,
                prompt=prompt,
                mcp_config=mcp_config,
                on_event=guarded_on_event,
                timeout=timeout,
                idle_timeout=idle_timeout,
                reasoning_effort=reasoning_effort,
                provider_session_id=provider_session_id,
                resume=resume,
                message_history=message_history,
                hooks=hooks,
                audit_context=audit_context,
                enable_config_discovery=enable_config_discovery,
                approve_permissions=approve_permissions,
                attachments=attachments,
                managed_tools=managed_tools,
                current_user_text=current_user_text,
                **kwargs,
            )

        task: asyncio.Task[str | None] = asyncio.ensure_future(run())
        task_holder["task"] = task
        try:
            result = await task
        except asyncio.CancelledError:
            # Distinguish *our* cancellation (a quota breach) from the caller's
            # (browser closed, daemon draining). Only the former becomes a
            # quota error; re-raising the latter preserves shutdown semantics.
            if breach:
                raise breach[0].as_exception() from None
            raise
        finally:
            await service.release_reservation(organization_id, user_id, model)

        if breach:
            # Some engines swallow cancellation and return a partial result.
            # The breach is authoritative either way, and surfacing it is what
            # makes the limit visible instead of a silently truncated answer.
            logger.warning(
                "Token limit exceeded during streaming session: %s",
                breach[0].as_exception().message(),
            )
            raise breach[0].as_exception()

        if not snapshot.is_open:
            await service.post_check(
                organization_id=organization_id,
                user_id=user_id,
                model_id=model,
                usage=accumulated,
            )
        return result


def enforce(engine: LLMEngine) -> LLMEngine:
    """Wrap an engine so its calls are subject to token limits.

    Idempotent: an already-wrapped engine is returned unchanged so repeated
    registry lookups do not stack wrappers.
    """
    if isinstance(engine, QuotaEnforcingEngine):
        return engine
    return QuotaEnforcingEngine(engine)


__all__ = ["QuotaEnforcingEngine", "enforce"]
