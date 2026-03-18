"""Slack integration adapter — webhook verification, event parsing, messaging.

Implements :class:`IntegrationAdapter` for Slack using raw HTTP via httpx
(no ``slack_sdk`` dependency — httpx is already in the project). The adapter
pattern means this can be swapped for a slack_sdk-backed implementation later
if the team decides to pull in the dependency.

Slack-specific behaviours:
- HMAC-SHA256 signature verification (v0 scheme)
- URL verification challenge (immediate JSON echo)
- Event parsing for ``message``, ``app_mention``, and slash commands
- Bot message filtering (``bot_id`` present or ``subtype == "bot_message"``)
- Markdown → Slack Block Kit formatting
- Message sending via ``chat.postMessage`` / ``chat.postEphemeral``
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Any

import httpx
from starlette.requests import Request

from lucent.integrations.base import IntegrationError
from lucent.integrations.models import EventType, IntegrationEvent
from lucent.logging import get_logger

logger = get_logger("integrations.slack")

# Slack rejects requests older than 5 minutes
_TIMESTAMP_MAX_AGE_SECONDS = 300

_SLACK_API_BASE = "https://slack.com/api"


class SlackAdapter:
    """Slack adapter implementing :class:`IntegrationAdapter`.

    Parameters
    ----------
    signing_secret:
        The Slack app's signing secret (used for HMAC-SHA256 verification).
    bot_token:
        The ``xoxb-`` bot token for sending messages.
    bot_user_id:
        The bot's own Slack user ID (e.g. ``U01ABC123``). Used to filter
        self-referencing events and strip mentions from text.
    """

    def __init__(
        self,
        *,
        signing_secret: str,
        bot_token: str,
        bot_user_id: str | None = None,
    ) -> None:
        if not signing_secret:
            raise ValueError("signing_secret is required")
        if not bot_token:
            raise ValueError("bot_token is required")
        self._signing_secret = signing_secret
        self._bot_token = bot_token
        self._bot_user_id = bot_user_id
        self._http = httpx.AsyncClient(
            base_url=_SLACK_API_BASE,
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=30.0,
        )

    # ------------------------------------------------------------------
    # IntegrationAdapter protocol
    # ------------------------------------------------------------------

    @property
    def platform(self) -> str:
        return "slack"

    async def verify_signature(self, request: Request) -> bool:
        """Verify Slack's ``X-Slack-Signature`` using HMAC-SHA256 (v0 scheme).

        Checks:
        1. Both signature and timestamp headers are present
        2. Timestamp is within ``_TIMESTAMP_MAX_AGE_SECONDS`` of now
        3. HMAC matches ``v0={hash}``
        """
        signature = request.headers.get("X-Slack-Signature")
        timestamp_str = request.headers.get("X-Slack-Request-Timestamp")

        if not signature or not timestamp_str:
            logger.debug("Missing signature headers")
            return False

        # Reject old timestamps to prevent replay attacks
        try:
            timestamp = int(timestamp_str)
        except ValueError:
            logger.debug("Non-integer timestamp: %s", timestamp_str)
            return False

        if abs(time.time() - timestamp) > _TIMESTAMP_MAX_AGE_SECONDS:
            logger.debug("Stale timestamp: %s", timestamp_str)
            return False

        body = await request.body()

        # v0:<timestamp>:<body>
        sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
        computed = (
            "v0="
            + hmac.new(
                self._signing_secret.encode("utf-8"),
                sig_basestring.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )

        return hmac.compare_digest(computed, signature)

    async def parse_event(self, request: Request) -> IntegrationEvent:
        """Parse a verified Slack webhook into a normalized IntegrationEvent.

        Handles three payload shapes:
        - ``url_verification`` — Slack's challenge handshake
        - ``event_callback`` — real-time events (message, app_mention, etc.)
        - Slash commands — form-encoded POST
        """
        content_type = request.headers.get("content-type", "")

        # Slash commands arrive as form-encoded, not JSON
        if "application/x-www-form-urlencoded" in content_type:
            return await self._parse_slash_command(request)

        body = await request.json()
        event_type = body.get("type", "")

        if event_type == "url_verification":
            return self._parse_url_verification(body)

        if event_type == "event_callback":
            return self._parse_event_callback(body)

        logger.warning("Unrecognized Slack payload type: %s", event_type)
        return IntegrationEvent(
            platform="slack",
            event_type=EventType.UNKNOWN,
            external_user_id="",
            channel_id="",
            text="",
            raw_payload=body,
        )

    async def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Send a message via Slack's ``chat.postMessage`` API.

        Returns the Slack message ``ts`` (timestamp ID).
        """
        payload: dict[str, Any] = {
            "channel": channel_id,
            **_markdown_to_blocks(content),
        }
        if thread_id:
            payload["thread_ts"] = thread_id

        endpoint = "chat.postMessage"

        # Support ephemeral messages via metadata
        if metadata and metadata.get("ephemeral"):
            user_id = metadata.get("user_id")
            if user_id:
                payload["user"] = user_id
                endpoint = "chat.postEphemeral"

        resp = await self._http.post(f"/{endpoint}", json=payload)
        resp.raise_for_status()
        data = resp.json()

        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            retryable = error in {
                "ratelimited",
                "service_unavailable",
                "request_timeout",
                "fatal_error",
            }
            logger.error("Slack API error: endpoint=%s, error=%s", endpoint, error)
            raise IntegrationError(
                f"Slack API error: {error}",
                platform="slack",
                retryable=retryable,
            )

        return data.get("ts", "")

    async def format_response(
        self,
        content: str,
        *,
        ephemeral: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Format a plain-text/Markdown response into a Slack-native payload.

        Returns a dict suitable for Slack's ``chat.postMessage`` or
        ``response_url`` callback.
        """
        payload: dict[str, Any] = {
            **_markdown_to_blocks(content),
        }
        if ephemeral:
            payload["response_type"] = "ephemeral"
        else:
            payload["response_type"] = "in_channel"
        return payload

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Private parsers
    # ------------------------------------------------------------------

    def _parse_url_verification(self, body: dict[str, Any]) -> IntegrationEvent:
        """Handle Slack's URL verification challenge."""
        return IntegrationEvent(
            platform="slack",
            event_type=EventType.URL_VERIFICATION,
            external_user_id="",
            channel_id="",
            text=body.get("challenge", ""),
            raw_payload=body,
        )

    def _parse_event_callback(self, body: dict[str, Any]) -> IntegrationEvent:
        """Parse an ``event_callback`` wrapper into a normalized event.

        Filters out bot messages (``bot_id`` present or
        ``subtype == "bot_message"``) by returning an event with
        ``EventType.UNKNOWN`` — downstream handlers should discard these.
        """
        event = body.get("event", {})
        event_subtype = event.get("type", "")
        workspace_id = body.get("team_id")

        user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts")
        ts = event.get("ts")

        # --- Bot message filtering ---
        if self._is_bot_message(event):
            logger.debug("Filtered bot message: channel=%s, ts=%s", channel_id, ts)
            return IntegrationEvent(
                platform="slack",
                event_type=EventType.UNKNOWN,
                external_user_id=user_id,
                channel_id=channel_id,
                text="",
                thread_id=thread_ts,
                external_workspace_id=workspace_id,
                raw_payload=body,
            )

        # Strip bot mention from text if present
        if self._bot_user_id and text:
            text = re.sub(
                rf"<@{re.escape(self._bot_user_id)}>\s*",
                "",
                text,
            ).strip()

        # Map Slack event types to normalized types
        if event_subtype in ("message", "app_mention"):
            normalized_type = EventType.MESSAGE
        else:
            normalized_type = EventType.UNKNOWN

        return IntegrationEvent(
            platform="slack",
            event_type=normalized_type,
            external_user_id=user_id,
            channel_id=channel_id,
            text=text,
            thread_id=thread_ts or ts,
            external_workspace_id=workspace_id,
            raw_payload=body,
        )

    async def _parse_slash_command(self, request: Request) -> IntegrationEvent:
        """Parse a Slack slash command (form-encoded POST)."""
        form = await request.form()
        return IntegrationEvent(
            platform="slack",
            event_type=EventType.COMMAND,
            external_user_id=str(form.get("user_id", "")),
            channel_id=str(form.get("channel_id", "")),
            text=str(form.get("text", "")),
            external_workspace_id=str(form.get("team_id", "")),
            raw_payload={
                "command": str(form.get("command", "")),
                "response_url": str(form.get("response_url", "")),
                "trigger_id": str(form.get("trigger_id", "")),
                "user_name": str(form.get("user_name", "")),
                "channel_name": str(form.get("channel_name", "")),
            },
        )

    def _is_bot_message(self, event: dict[str, Any]) -> bool:
        """Determine if an event was sent by a bot (should be filtered)."""
        # Explicit bot_id field
        if event.get("bot_id"):
            return True
        # Subtype indicates bot
        if event.get("subtype") == "bot_message":
            return True
        # Our own bot user
        if self._bot_user_id and event.get("user") == self._bot_user_id:
            return True
        return False


# ======================================================================
# Markdown → Slack Block Kit conversion
# ======================================================================


def _markdown_to_blocks(content: str) -> dict[str, Any]:
    """Convert Markdown content to Slack Block Kit payload.

    Slack's ``mrkdwn`` format is close to Markdown but has differences:
    - Bold: ``*text*`` (not ``**text**``)
    - Italic: ``_text_`` (same)
    - Strikethrough: ``~text~`` (same)
    - Code blocks: triple backticks (same)
    - Links: ``<url|text>`` (not ``[text](url)``)

    Returns a dict with ``blocks`` and ``text`` (fallback) keys.
    """
    if not content:
        return {"blocks": [], "text": ""}

    slack_text = _convert_markdown_to_mrkdwn(content)
    blocks = _text_to_section_blocks(slack_text)

    return {
        "blocks": blocks,
        "text": content[:3000],  # Fallback for notifications / accessibility
    }


def _convert_markdown_to_mrkdwn(text: str) -> str:
    """Transform standard Markdown syntax to Slack mrkdwn."""
    # Links: [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # Bold: **text** → *text* (but not inside code blocks)
    # Process outside of code spans/blocks
    text = _transform_outside_code(text, r"\*\*(.+?)\*\*", r"*\1*")

    # Headers: # Header → *Header* (bold, since Slack has no headers in mrkdwn)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    return text


def _transform_outside_code(text: str, pattern: str, replacement: str) -> str:
    """Apply a regex substitution only outside of code fences and inline code."""
    parts = re.split(r"(```[\s\S]*?```|`[^`]+`)", text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            # Outside code — apply transform
            result.append(re.sub(pattern, replacement, part))
        else:
            # Inside code — leave as-is
            result.append(part)
    return "".join(result)


def _text_to_section_blocks(text: str) -> list[dict[str, Any]]:
    """Split text into Slack section blocks, respecting the 3000-char limit.

    Code blocks (````` ``` `````) become their own sections to preserve
    formatting.
    """
    blocks: list[dict[str, Any]] = []

    # Split on code fences, keeping the delimiters
    segments = re.split(r"(```[\s\S]*?```)", text)

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue

        # Slack section text limit is 3000 chars
        for chunk in _chunk_text(segment, 3000):
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": chunk},
                }
            )

    return blocks


def _chunk_text(text: str, max_len: int) -> list[str]:
    """Split text into chunks of at most ``max_len`` characters.

    Tries to split on newlines to preserve readability.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        # Find last newline within the limit
        split_at = text.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = max_len

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    return chunks
