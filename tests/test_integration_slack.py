"""Tests for lucent.integrations.slack_adapter — SlackAdapter."""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from lucent.integrations.base import IntegrationAdapter, IntegrationError
from lucent.integrations.models import EventType
from lucent.integrations.slack_adapter import (
    SlackAdapter,
    _chunk_text,
    _convert_markdown_to_mrkdwn,
    _markdown_to_blocks,
    _text_to_section_blocks,
    _transform_outside_code,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SIGNING_SECRET = "test_secret_12345"
BOT_TOKEN = "xoxb-test-token-12345"
BOT_USER_ID = "U0BOTUSER"


@pytest.fixture()
def adapter() -> SlackAdapter:
    return SlackAdapter(
        signing_secret=SIGNING_SECRET,
        bot_token=BOT_TOKEN,
        bot_user_id=BOT_USER_ID,
    )


def _make_slack_request(
    body: bytes,
    *,
    signing_secret: str = SIGNING_SECRET,
    timestamp: int | None = None,
    tamper_signature: bool = False,
    omit_headers: bool = False,
) -> MagicMock:
    """Build a mock Starlette Request with Slack signature headers."""
    ts = timestamp or int(time.time())
    sig_basestring = f"v0:{ts}:{body.decode('utf-8')}"
    sig = (
        "v0="
        + hmac_mod.new(
            signing_secret.encode(),
            sig_basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    if tamper_signature:
        sig = "v0=" + "0" * 64

    headers: dict[str, str] = {}
    if not omit_headers:
        headers["X-Slack-Signature"] = sig
        headers["X-Slack-Request-Timestamp"] = str(ts)
    headers["content-type"] = "application/json"

    request = MagicMock()
    request.headers = headers
    request.body = AsyncMock(return_value=body)
    return request


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestSlackAdapterInit:
    def test_valid_construction(self) -> None:
        a = SlackAdapter(signing_secret="sec", bot_token="tok")
        assert a.platform == "slack"

    def test_missing_signing_secret(self) -> None:
        with pytest.raises(ValueError, match="signing_secret"):
            SlackAdapter(signing_secret="", bot_token="tok")

    def test_missing_bot_token(self) -> None:
        with pytest.raises(ValueError, match="bot_token"):
            SlackAdapter(signing_secret="sec", bot_token="")


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


class TestVerifySignature:
    @pytest.mark.asyncio
    async def test_valid_signature(self, adapter: SlackAdapter) -> None:
        body = b'{"type":"event_callback"}'
        request = _make_slack_request(body)
        assert await adapter.verify_signature(request) is True

    @pytest.mark.asyncio
    async def test_tampered_signature(self, adapter: SlackAdapter) -> None:
        body = b'{"type":"event_callback"}'
        request = _make_slack_request(body, tamper_signature=True)
        assert await adapter.verify_signature(request) is False

    @pytest.mark.asyncio
    async def test_missing_headers(self, adapter: SlackAdapter) -> None:
        body = b'{"type":"event_callback"}'
        request = _make_slack_request(body, omit_headers=True)
        assert await adapter.verify_signature(request) is False

    @pytest.mark.asyncio
    async def test_stale_timestamp(self, adapter: SlackAdapter) -> None:
        body = b'{"type":"event_callback"}'
        old_ts = int(time.time()) - 600  # 10 minutes ago
        request = _make_slack_request(body, timestamp=old_ts)
        assert await adapter.verify_signature(request) is False

    @pytest.mark.asyncio
    async def test_wrong_signing_secret(self) -> None:
        adapter = SlackAdapter(
            signing_secret="different_secret",
            bot_token=BOT_TOKEN,
        )
        body = b'{"type":"event_callback"}'
        request = _make_slack_request(body, signing_secret=SIGNING_SECRET)
        assert await adapter.verify_signature(request) is False

    @pytest.mark.asyncio
    async def test_non_integer_timestamp(self, adapter: SlackAdapter) -> None:
        request = MagicMock()
        request.headers = {
            "X-Slack-Signature": "v0=abc",
            "X-Slack-Request-Timestamp": "not-a-number",
        }
        request.body = AsyncMock(return_value=b"")
        assert await adapter.verify_signature(request) is False


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------


class TestParseEvent:
    @pytest.mark.asyncio
    async def test_url_verification(self, adapter: SlackAdapter) -> None:
        body = {"type": "url_verification", "challenge": "test_challenge_abc"}
        request = MagicMock()
        request.headers = {"content-type": "application/json"}
        request.json = AsyncMock(return_value=body)

        event = await adapter.parse_event(request)
        assert event.event_type == EventType.URL_VERIFICATION
        assert event.text == "test_challenge_abc"
        assert event.platform == "slack"

    @pytest.mark.asyncio
    async def test_event_callback_message(self, adapter: SlackAdapter) -> None:
        body = {
            "type": "event_callback",
            "team_id": "T123",
            "event": {
                "type": "message",
                "user": "U456",
                "channel": "C789",
                "text": "hello world",
                "ts": "1234.5678",
            },
        }
        request = MagicMock()
        request.headers = {"content-type": "application/json"}
        request.json = AsyncMock(return_value=body)

        event = await adapter.parse_event(request)
        assert event.event_type == EventType.MESSAGE
        assert event.external_user_id == "U456"
        assert event.channel_id == "C789"
        assert event.text == "hello world"
        assert event.external_workspace_id == "T123"

    @pytest.mark.asyncio
    async def test_bot_message_filtered(self, adapter: SlackAdapter) -> None:
        body = {
            "type": "event_callback",
            "team_id": "T123",
            "event": {
                "type": "message",
                "user": "U456",
                "channel": "C789",
                "text": "bot says hi",
                "bot_id": "B999",
                "ts": "1234.5678",
            },
        }
        request = MagicMock()
        request.headers = {"content-type": "application/json"}
        request.json = AsyncMock(return_value=body)

        event = await adapter.parse_event(request)
        assert event.event_type == EventType.UNKNOWN
        assert event.text == ""

    @pytest.mark.asyncio
    async def test_own_bot_message_filtered(self, adapter: SlackAdapter) -> None:
        body = {
            "type": "event_callback",
            "team_id": "T123",
            "event": {
                "type": "message",
                "user": BOT_USER_ID,
                "channel": "C789",
                "text": "my own message",
                "ts": "1234.5678",
            },
        }
        request = MagicMock()
        request.headers = {"content-type": "application/json"}
        request.json = AsyncMock(return_value=body)

        event = await adapter.parse_event(request)
        assert event.event_type == EventType.UNKNOWN

    @pytest.mark.asyncio
    async def test_mention_stripped(self, adapter: SlackAdapter) -> None:
        body = {
            "type": "event_callback",
            "team_id": "T123",
            "event": {
                "type": "app_mention",
                "user": "U456",
                "channel": "C789",
                "text": f"<@{BOT_USER_ID}> do something",
                "ts": "1234.5678",
            },
        }
        request = MagicMock()
        request.headers = {"content-type": "application/json"}
        request.json = AsyncMock(return_value=body)

        event = await adapter.parse_event(request)
        assert event.event_type == EventType.MESSAGE
        assert event.text == "do something"

    @pytest.mark.asyncio
    async def test_slash_command(self, adapter: SlackAdapter) -> None:
        mock_form = {
            "user_id": "U456",
            "channel_id": "C789",
            "text": "query something",
            "team_id": "T123",
            "command": "/lucent",
            "response_url": "https://hooks.slack.com/...",
            "trigger_id": "TRG1",
            "user_name": "testuser",
            "channel_name": "general",
        }
        request = MagicMock()
        request.headers = {"content-type": "application/x-www-form-urlencoded"}
        request.form = AsyncMock(return_value=mock_form)

        event = await adapter.parse_event(request)
        assert event.event_type == EventType.COMMAND
        assert event.external_user_id == "U456"
        assert event.text == "query something"
        assert event.raw_payload["command"] == "/lucent"

    @pytest.mark.asyncio
    async def test_unknown_event_type(self, adapter: SlackAdapter) -> None:
        body = {"type": "some_future_event"}
        request = MagicMock()
        request.headers = {"content-type": "application/json"}
        request.json = AsyncMock(return_value=body)

        event = await adapter.parse_event(request)
        assert event.event_type == EventType.UNKNOWN


# ---------------------------------------------------------------------------
# Markdown conversion
# ---------------------------------------------------------------------------


class TestMarkdownToBlocks:
    def test_empty_content(self) -> None:
        result = _markdown_to_blocks("")
        assert result["blocks"] == []
        assert result["text"] == ""

    def test_simple_text(self) -> None:
        result = _markdown_to_blocks("Hello world")
        assert len(result["blocks"]) == 1
        assert result["blocks"][0]["type"] == "section"
        assert result["blocks"][0]["text"]["text"] == "Hello world"

    def test_bold_conversion(self) -> None:
        result = _convert_markdown_to_mrkdwn("This is **bold** text")
        assert result == "This is *bold* text"

    def test_link_conversion(self) -> None:
        result = _convert_markdown_to_mrkdwn("[click here](https://example.com)")
        assert result == "<https://example.com|click here>"

    def test_header_conversion(self) -> None:
        result = _convert_markdown_to_mrkdwn("# My Header")
        assert result == "*My Header*"

    def test_code_blocks_preserved(self) -> None:
        text = "Before **bold** ```code **not bold**``` after **bold**"
        result = _convert_markdown_to_mrkdwn(text)
        assert "```code **not bold**```" in result
        assert "*bold*" in result


class TestChunkText:
    def test_short_text_no_split(self) -> None:
        assert _chunk_text("hello", 100) == ["hello"]

    def test_exact_limit(self) -> None:
        text = "x" * 100
        assert _chunk_text(text, 100) == [text]

    def test_split_on_newline(self) -> None:
        text = "line1\nline2\nline3"
        chunks = _chunk_text(text, 10)
        assert len(chunks) >= 2
        assert all(len(c) <= 10 for c in chunks)

    def test_long_text_hard_split(self) -> None:
        text = "x" * 200
        chunks = _chunk_text(text, 100)
        assert len(chunks) == 2
        assert chunks[0] == "x" * 100
        assert chunks[1] == "x" * 100


# ---------------------------------------------------------------------------
# Message sending
# ---------------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_success(self, adapter: SlackAdapter) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "ts": "1234.5678"}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(adapter._http, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            ts = await adapter.send_message("C123", "hello")
            assert ts == "1234.5678"
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "/chat.postMessage" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_send_ephemeral(self, adapter: SlackAdapter) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "ts": "1234.5678"}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(adapter._http, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            await adapter.send_message(
                "C123", "secret", metadata={"ephemeral": True, "user_id": "U1"}
            )
            call_args = mock_post.call_args
            assert "/chat.postEphemeral" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_send_api_error_retryable(self, adapter: SlackAdapter) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": False, "error": "ratelimited"}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(adapter._http, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            with pytest.raises(IntegrationError) as exc_info:
                await adapter.send_message("C123", "hello")
            assert exc_info.value.retryable is True
            assert exc_info.value.platform == "slack"

    @pytest.mark.asyncio
    async def test_send_api_error_not_retryable(self, adapter: SlackAdapter) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": False, "error": "channel_not_found"}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(adapter._http, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            with pytest.raises(IntegrationError) as exc_info:
                await adapter.send_message("C123", "hello")
            assert exc_info.value.retryable is False

    @pytest.mark.asyncio
    async def test_send_with_thread(self, adapter: SlackAdapter) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "ts": "5678.9012"}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(adapter._http, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            await adapter.send_message("C123", "reply", thread_id="1234.5678")
            payload = mock_post.call_args[1]["json"]
            assert payload["thread_ts"] == "1234.5678"


# ---------------------------------------------------------------------------
# Format response
# ---------------------------------------------------------------------------


class TestFormatResponse:
    @pytest.mark.asyncio
    async def test_in_channel(self, adapter: SlackAdapter) -> None:
        result = await adapter.format_response("hello")
        assert result["response_type"] == "in_channel"
        assert "blocks" in result

    @pytest.mark.asyncio
    async def test_ephemeral(self, adapter: SlackAdapter) -> None:
        result = await adapter.format_response("secret", ephemeral=True)
        assert result["response_type"] == "ephemeral"


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    def test_platform_property(self, adapter: SlackAdapter) -> None:
        assert adapter.platform == "slack"

    @pytest.mark.asyncio
    async def test_close(self, adapter: SlackAdapter) -> None:
        with patch.object(adapter._http, "aclose", new_callable=AsyncMock) as mock_close:
            await adapter.close()
            mock_close.assert_called_once()

    def test_satisfies_integration_adapter_protocol(self, adapter: SlackAdapter) -> None:
        assert isinstance(adapter, IntegrationAdapter)


# ---------------------------------------------------------------------------
# Extended HMAC signature verification
# ---------------------------------------------------------------------------


class TestVerifySignatureExtended:
    """Additional edge cases for HMAC verification."""

    @pytest.mark.asyncio
    async def test_missing_signature_only(self, adapter: SlackAdapter) -> None:
        """Timestamp present but signature missing → reject."""
        request = MagicMock()
        request.headers = {
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "content-type": "application/json",
        }
        request.body = AsyncMock(return_value=b"{}")
        assert await adapter.verify_signature(request) is False

    @pytest.mark.asyncio
    async def test_missing_timestamp_only(self, adapter: SlackAdapter) -> None:
        """Signature present but timestamp missing → reject."""
        request = MagicMock()
        request.headers = {
            "X-Slack-Signature": "v0=abc",
            "content-type": "application/json",
        }
        request.body = AsyncMock(return_value=b"{}")
        assert await adapter.verify_signature(request) is False

    @pytest.mark.asyncio
    async def test_future_timestamp_within_window(self, adapter: SlackAdapter) -> None:
        """Timestamp 250s in the future — within 300s window → accept."""
        body = b'{"type":"event_callback"}'
        future_ts = int(time.time()) + 250
        request = _make_slack_request(body, timestamp=future_ts)
        assert await adapter.verify_signature(request) is True

    @pytest.mark.asyncio
    async def test_future_timestamp_outside_window(self, adapter: SlackAdapter) -> None:
        """Timestamp 400s in the future — outside 300s window → reject."""
        body = b'{"type":"event_callback"}'
        future_ts = int(time.time()) + 400
        request = _make_slack_request(body, timestamp=future_ts)
        assert await adapter.verify_signature(request) is False

    @pytest.mark.asyncio
    async def test_empty_body_valid_sig(self, adapter: SlackAdapter) -> None:
        """Empty body with a correctly computed signature should verify."""
        body = b""
        request = _make_slack_request(body)
        assert await adapter.verify_signature(request) is True


# ---------------------------------------------------------------------------
# Extended event parsing
# ---------------------------------------------------------------------------


class TestParseEventExtended:
    """Additional event parsing scenarios."""

    @pytest.mark.asyncio
    async def test_bot_subtype_filtered(self, adapter: SlackAdapter) -> None:
        """subtype=bot_message with no bot_id still gets filtered."""
        body = {
            "type": "event_callback",
            "team_id": "T123",
            "event": {
                "type": "message",
                "user": "U999",
                "channel": "C789",
                "text": "bot via subtype",
                "subtype": "bot_message",
                "ts": "1234.5678",
            },
        }
        request = MagicMock()
        request.headers = {"content-type": "application/json"}
        request.json = AsyncMock(return_value=body)

        event = await adapter.parse_event(request)
        assert event.event_type == EventType.UNKNOWN
        assert event.text == ""

    @pytest.mark.asyncio
    async def test_thread_ts_preserved(self, adapter: SlackAdapter) -> None:
        """thread_ts from Slack event is passed through as thread_id."""
        body = {
            "type": "event_callback",
            "team_id": "T123",
            "event": {
                "type": "message",
                "user": "U456",
                "channel": "C789",
                "text": "reply in thread",
                "ts": "1234.5678",
                "thread_ts": "1111.0000",
            },
        }
        request = MagicMock()
        request.headers = {"content-type": "application/json"}
        request.json = AsyncMock(return_value=body)

        event = await adapter.parse_event(request)
        assert event.thread_id == "1111.0000"

    @pytest.mark.asyncio
    async def test_event_ts_used_when_no_thread(self, adapter: SlackAdapter) -> None:
        """When no thread_ts, the message ts becomes thread_id."""
        body = {
            "type": "event_callback",
            "team_id": "T123",
            "event": {
                "type": "message",
                "user": "U456",
                "channel": "C789",
                "text": "top level",
                "ts": "1234.5678",
            },
        }
        request = MagicMock()
        request.headers = {"content-type": "application/json"}
        request.json = AsyncMock(return_value=body)

        event = await adapter.parse_event(request)
        assert event.thread_id == "1234.5678"

    @pytest.mark.asyncio
    async def test_adapter_without_bot_user_id(self) -> None:
        """Adapter with bot_user_id=None doesn't filter by user match."""
        a = SlackAdapter(signing_secret="sec", bot_token="tok", bot_user_id=None)
        body = {
            "type": "event_callback",
            "team_id": "T123",
            "event": {
                "type": "message",
                "user": "UANYONE",
                "channel": "C789",
                "text": "hi there",
                "ts": "1234.5678",
            },
        }
        request = MagicMock()
        request.headers = {"content-type": "application/json"}
        request.json = AsyncMock(return_value=body)

        event = await a.parse_event(request)
        assert event.event_type == EventType.MESSAGE
        assert event.text == "hi there"

    @pytest.mark.asyncio
    async def test_mention_not_stripped_without_bot_user_id(self) -> None:
        """Without bot_user_id, mention text is left intact."""
        a = SlackAdapter(signing_secret="sec", bot_token="tok", bot_user_id=None)
        body = {
            "type": "event_callback",
            "team_id": "T123",
            "event": {
                "type": "app_mention",
                "user": "U456",
                "channel": "C789",
                "text": "<@UFOO> do something",
                "ts": "1234.5678",
            },
        }
        request = MagicMock()
        request.headers = {"content-type": "application/json"}
        request.json = AsyncMock(return_value=body)

        event = await a.parse_event(request)
        assert "<@UFOO>" in event.text

    @pytest.mark.asyncio
    async def test_slash_command_raw_payload_fields(self, adapter: SlackAdapter) -> None:
        """Verify all raw_payload fields in slash command parsing."""
        mock_form = {
            "user_id": "U456",
            "channel_id": "C789",
            "text": "query",
            "team_id": "T123",
            "command": "/lucent",
            "response_url": "https://hooks.slack.com/resp",
            "trigger_id": "TRG1",
            "user_name": "testuser",
            "channel_name": "general",
        }
        request = MagicMock()
        request.headers = {"content-type": "application/x-www-form-urlencoded"}
        request.form = AsyncMock(return_value=mock_form)

        event = await adapter.parse_event(request)
        assert event.raw_payload["response_url"] == "https://hooks.slack.com/resp"
        assert event.raw_payload["trigger_id"] == "TRG1"
        assert event.raw_payload["user_name"] == "testuser"
        assert event.raw_payload["channel_name"] == "general"

    @pytest.mark.asyncio
    async def test_url_verification_returns_challenge(self, adapter: SlackAdapter) -> None:
        """URL verification event includes challenge in text field."""
        body = {"type": "url_verification", "challenge": "xyz_challenge_token"}
        request = MagicMock()
        request.headers = {"content-type": "application/json"}
        request.json = AsyncMock(return_value=body)

        event = await adapter.parse_event(request)
        assert event.event_type == EventType.URL_VERIFICATION
        assert event.text == "xyz_challenge_token"
        assert event.external_user_id == ""
        assert event.channel_id == ""


# ---------------------------------------------------------------------------
# Extended bot message filtering
# ---------------------------------------------------------------------------


class TestIsBotMessage:
    """Direct _is_bot_message() coverage."""

    def test_bot_id_present(self, adapter: SlackAdapter) -> None:
        assert adapter._is_bot_message({"bot_id": "B123"}) is True

    def test_bot_subtype(self, adapter: SlackAdapter) -> None:
        assert adapter._is_bot_message({"subtype": "bot_message"}) is True

    def test_own_bot_user(self, adapter: SlackAdapter) -> None:
        assert adapter._is_bot_message({"user": BOT_USER_ID}) is True

    def test_regular_user(self, adapter: SlackAdapter) -> None:
        assert adapter._is_bot_message({"user": "U_HUMAN"}) is False

    def test_empty_event(self, adapter: SlackAdapter) -> None:
        assert adapter._is_bot_message({}) is False

    def test_no_bot_user_id_configured(self) -> None:
        a = SlackAdapter(signing_secret="sec", bot_token="tok", bot_user_id=None)
        assert a._is_bot_message({"user": "UFOO"}) is False


# ---------------------------------------------------------------------------
# Extended retryable error classification
# ---------------------------------------------------------------------------


class TestRetryableErrors:
    """Verify all retryable Slack error codes and non-retryable codes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_code",
        ["ratelimited", "service_unavailable", "request_timeout", "fatal_error"],
    )
    async def test_retryable_errors(self, adapter: SlackAdapter, error_code: str) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": False, "error": error_code}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(adapter._http, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            with pytest.raises(IntegrationError) as exc_info:
                await adapter.send_message("C123", "hello")
            assert exc_info.value.retryable is True
            assert exc_info.value.platform == "slack"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_code",
        [
            "channel_not_found",
            "not_authed",
            "invalid_auth",
            "account_inactive",
            "no_text",
        ],
    )
    async def test_non_retryable_errors(self, adapter: SlackAdapter, error_code: str) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": False, "error": error_code}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(adapter._http, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            with pytest.raises(IntegrationError) as exc_info:
                await adapter.send_message("C123", "hello")
            assert exc_info.value.retryable is False

    @pytest.mark.asyncio
    async def test_http_transport_error(self, adapter: SlackAdapter) -> None:
        """httpx transport error propagates (not swallowed)."""
        with patch.object(adapter._http, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = httpx.ConnectError("connection refused")
            with pytest.raises(httpx.ConnectError):
                await adapter.send_message("C123", "hello")

    @pytest.mark.asyncio
    async def test_send_returns_empty_ts_on_missing(self, adapter: SlackAdapter) -> None:
        """If response has ok=True but no ts, returns empty string."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(adapter._http, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            ts = await adapter.send_message("C123", "hello")
            assert ts == ""


# ---------------------------------------------------------------------------
# Extended markdown conversion
# ---------------------------------------------------------------------------


class TestMarkdownConversionExtended:
    def test_h2_header(self) -> None:
        assert _convert_markdown_to_mrkdwn("## Sub Header") == "*Sub Header*"

    def test_h3_header(self) -> None:
        assert _convert_markdown_to_mrkdwn("### Deep Header") == "*Deep Header*"

    def test_inline_code_preserved(self) -> None:
        result = _convert_markdown_to_mrkdwn("Use `**not bold**` for emphasis")
        assert "`**not bold**`" in result

    def test_multiple_links(self) -> None:
        text = "[A](https://a.com) and [B](https://b.com)"
        result = _convert_markdown_to_mrkdwn(text)
        assert "<https://a.com|A>" in result
        assert "<https://b.com|B>" in result

    def test_bold_not_converted_inside_code_fence(self) -> None:
        text = "```\n**stay bold markdown**\n```"
        result = _convert_markdown_to_mrkdwn(text)
        assert "**stay bold markdown**" in result

    def test_multiline_with_header_and_bold(self) -> None:
        text = "# Title\n\nThis is **important** text."
        result = _convert_markdown_to_mrkdwn(text)
        assert "*Title*" in result
        assert "*important*" in result


class TestTransformOutsideCode:
    def test_transform_skips_inline_code(self) -> None:
        result = _transform_outside_code("A `**B**` C **D**", r"\*\*(.+?)\*\*", r"*\1*")
        assert "`**B**`" in result
        assert "*D*" in result

    def test_transform_skips_fenced_code(self) -> None:
        result = _transform_outside_code(
            "**bold** ```**not**``` **bold**", r"\*\*(.+?)\*\*", r"*\1*"
        )
        assert result.startswith("*bold*")
        assert "```**not**```" in result
        assert result.endswith("*bold*")

    def test_no_code_blocks(self) -> None:
        result = _transform_outside_code("**all bold**", r"\*\*(.+?)\*\*", r"*\1*")
        assert result == "*all bold*"


class TestTextToSectionBlocks:
    def test_code_block_becomes_own_section(self) -> None:
        text = "Before\n```\ncode here\n```\nAfter"
        blocks = _text_to_section_blocks(text)
        assert len(blocks) == 3
        texts = [b["text"]["text"] for b in blocks]
        assert any("code here" in t for t in texts)

    def test_long_text_split_into_multiple_blocks(self) -> None:
        text = "x\n" * 2000  # Well over 3000 chars
        blocks = _text_to_section_blocks(text)
        assert len(blocks) >= 2
        for block in blocks:
            assert len(block["text"]["text"]) <= 3000

    def test_empty_segments_skipped(self) -> None:
        text = "```code```"
        blocks = _text_to_section_blocks(text)
        assert len(blocks) == 1
        assert "code" in blocks[0]["text"]["text"]

    def test_all_blocks_are_section_type(self) -> None:
        blocks = _text_to_section_blocks("hello\n```code```\nworld")
        for block in blocks:
            assert block["type"] == "section"
            assert block["text"]["type"] == "mrkdwn"


class TestMarkdownToBlocksFull:
    def test_fallback_text_truncated(self) -> None:
        long_content = "x" * 5000
        result = _markdown_to_blocks(long_content)
        assert len(result["text"]) == 3000

    def test_blocks_and_text_both_present(self) -> None:
        result = _markdown_to_blocks("Hello **world**")
        assert "blocks" in result
        assert "text" in result
        assert len(result["blocks"]) >= 1
