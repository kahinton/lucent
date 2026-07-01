"""Multimodal attachment handling for chat sessions.

This module is the single source of truth for how user-supplied attachments
(images and documents) are validated, persisted, and converted into the
provider-native formats expected by each LLM engine.

Design notes
------------
* **Normalized form** — Throughout Lucent an attachment is represented as a
  plain dict (:data:`NormalizedAttachment`) with ``kind``, ``mime_type``,
  ``name``, ``size`` and base64 ``data`` (no ``data:`` URL prefix). The API
  layer normalizes inbound payloads once; engines and storage consume that
  single shape.
* **Storage seam** — :class:`AttachmentStore` abstracts *how* attachment bytes
  are persisted alongside a message. The default :class:`InlineAttachmentStore`
  embeds base64 directly in the message metadata (no extra infrastructure). To
  move to object storage later, implement a new store and swap the return value
  of :func:`get_attachment_store` — nothing else changes.
* **Engine conversion** — :func:`to_copilot_blobs` and
  :func:`to_langchain_blocks` translate the normalized form into the
  GitHub Copilot SDK ``BlobAttachment`` and LangChain content-block formats.
"""

from __future__ import annotations

import base64
import binascii
import re
from abc import ABC, abstractmethod
from typing import Any, Literal

from lucent.logging import get_logger

logger = get_logger("llm.attachments")

# ── Limits & allow-lists ──────────────────────────────────────────────────
MAX_ATTACHMENTS = 8
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB per image
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024  # 10 MB per document
MAX_NAME_LENGTH = 256

IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}
DOCUMENT_MIME_TYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/csv",
}
ALLOWED_MIME_TYPES = IMAGE_MIME_TYPES | DOCUMENT_MIME_TYPES

# Document MIME types whose payload is UTF-8 text (inlined as text for engines
# that cannot accept raw document blocks).
TEXT_DOCUMENT_MIME_TYPES = {
    "text/plain",
    "text/markdown",
    "text/csv",
}

AttachmentKind = Literal["image", "document"]

# A normalized attachment is a JSON-serializable dict with these keys:
#   kind: "image" | "document"
#   mime_type: str
#   name: str
#   size: int            (decoded byte length)
#   data: str            (base64-encoded payload, no data: URL prefix)
NormalizedAttachment = dict[str, Any]

_DATA_URL_RE = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?:;[\w-]+=[\w-]+)*;base64,", re.IGNORECASE)


class AttachmentError(ValueError):
    """Raised when an attachment payload fails validation."""


def kind_for_mime(mime_type: str) -> AttachmentKind:
    """Return the attachment kind for a MIME type."""
    return "image" if mime_type in IMAGE_MIME_TYPES else "document"


def _strip_data_url(data: str) -> tuple[str, str | None]:
    """Strip a leading ``data:<mime>;base64,`` prefix if present.

    Returns ``(base64_payload, mime_from_url_or_None)``.
    """
    match = _DATA_URL_RE.match(data)
    if not match:
        return data.strip(), None
    return data[match.end():].strip(), (match.group("mime") or None)


def normalize_attachment(raw: dict[str, Any]) -> NormalizedAttachment:
    """Validate and normalize a single inbound attachment payload.

    Accepts either raw base64 in ``data`` or a full ``data:`` URL. The MIME
    type may be supplied explicitly via ``mime_type`` or inferred from a
    ``data:`` URL prefix.

    Raises:
        AttachmentError: if the attachment is missing data, has an unsupported
            MIME type, is not valid base64, or exceeds the size limit.
    """
    if not isinstance(raw, dict):
        raise AttachmentError("Attachment must be an object")

    data_field = raw.get("data")
    if not isinstance(data_field, str) or not data_field.strip():
        raise AttachmentError("Attachment is missing base64 data")

    b64, url_mime = _strip_data_url(data_field)
    mime_type = (raw.get("mime_type") or url_mime or "").strip().lower()
    if not mime_type:
        raise AttachmentError("Attachment is missing a MIME type")
    if mime_type not in ALLOWED_MIME_TYPES:
        raise AttachmentError(f"Unsupported attachment type: {mime_type}")

    try:
        decoded = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError("Attachment data is not valid base64") from exc
    if not decoded:
        raise AttachmentError("Attachment is empty")

    kind = kind_for_mime(mime_type)
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_DOCUMENT_BYTES
    if len(decoded) > limit:
        raise AttachmentError(
            f"Attachment exceeds the {limit // (1024 * 1024)} MB limit for {kind}s"
        )

    name = str(raw.get("name") or "").strip()[:MAX_NAME_LENGTH] or _default_name(kind, mime_type)

    # Re-encode from the validated bytes so what we store is canonical base64
    # regardless of how the client framed the payload.
    return {
        "kind": kind,
        "mime_type": mime_type,
        "name": name,
        "size": len(decoded),
        "data": base64.b64encode(decoded).decode("ascii"),
    }


def normalize_attachments(raw_list: Any) -> list[NormalizedAttachment]:
    """Validate and normalize a list of inbound attachments.

    Returns an empty list when ``raw_list`` is falsy. Raises
    :class:`AttachmentError` if the count exceeds :data:`MAX_ATTACHMENTS` or any
    individual attachment is invalid.
    """
    if not raw_list:
        return []
    if not isinstance(raw_list, list):
        raise AttachmentError("attachments must be a list")
    if len(raw_list) > MAX_ATTACHMENTS:
        raise AttachmentError(f"Too many attachments (max {MAX_ATTACHMENTS})")
    return [normalize_attachment(item) for item in raw_list]


def _default_name(kind: AttachmentKind, mime_type: str) -> str:
    ext = mime_type.split("/")[-1]
    return f"{kind}.{ext}"


def data_url(attachment: NormalizedAttachment) -> str:
    """Return a ``data:`` URL for a normalized attachment."""
    return f"data:{attachment['mime_type']};base64,{attachment['data']}"


def attachment_summary(attachments: list[NormalizedAttachment]) -> list[dict[str, Any]]:
    """Return lightweight metadata (no payload) for logging/auditing."""
    return [
        {"kind": a.get("kind"), "mime_type": a.get("mime_type"), "name": a.get("name"), "size": a.get("size")}
        for a in attachments
    ]


# ── Storage seam ──────────────────────────────────────────────────────────
class AttachmentStore(ABC):
    """Strategy for persisting attachment bytes alongside a message.

    ``persist`` converts normalized attachments into the JSON shape stored in
    ``llm_messages.metadata``; ``load`` reverses that for re-hydration. Keeping
    both directions here means callers never assume bytes live inline.
    """

    @abstractmethod
    async def persist(self, attachments: list[NormalizedAttachment]) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    async def load(self, stored: list[dict[str, Any]]) -> list[NormalizedAttachment]:
        ...


class InlineAttachmentStore(AttachmentStore):
    """Default store: embed base64 payloads directly in message metadata.

    Requires no extra infrastructure. The persisted shape is identical to the
    normalized shape, so ``load`` is the identity transform.
    """

    async def persist(self, attachments: list[NormalizedAttachment]) -> list[dict[str, Any]]:
        return [dict(a) for a in attachments]

    async def load(self, stored: list[dict[str, Any]]) -> list[NormalizedAttachment]:
        return [dict(a) for a in stored if isinstance(a, dict) and a.get("data")]


_store: AttachmentStore | None = None


def get_attachment_store() -> AttachmentStore:
    """Return the configured attachment store (singleton).

    Swap the constructed type here (e.g. an object-storage backend) to change
    how attachments are persisted without touching the API, engines, or UI.
    """
    global _store
    if _store is None:
        _store = InlineAttachmentStore()
    return _store


# ── Engine conversions ────────────────────────────────────────────────────
def to_copilot_blobs(attachments: list[NormalizedAttachment]) -> list[dict[str, Any]]:
    """Convert normalized attachments into Copilot SDK ``BlobAttachment`` dicts.

    Text documents are excluded — Copilot models do not read text blobs, so
    those are inlined into the prompt via :func:`inline_text_documents`
    instead. Images and binary documents (e.g. PDF) are sent as blobs.
    """
    blobs: list[dict[str, Any]] = []
    for a in attachments:
        if a["kind"] != "image" and a["mime_type"] in TEXT_DOCUMENT_MIME_TYPES:
            continue
        blobs.append(
            {
                "type": "blob",
                "data": a["data"],
                "mimeType": a["mime_type"],
                "displayName": a.get("name") or _default_name(a["kind"], a["mime_type"]),
            }
        )
    return blobs


def inline_text_documents(
    prompt: str,
    attachments: list[NormalizedAttachment],
) -> str:
    """Append decoded text-document attachments to ``prompt``.

    Used by engines (e.g. Copilot) that cannot read text documents as binary
    blobs. Images and binary documents are left for the blob path.
    """
    texts = [
        _inline_text_document(a)
        for a in attachments
        if a["kind"] != "image" and a["mime_type"] in TEXT_DOCUMENT_MIME_TYPES
    ]
    if not texts:
        return prompt
    joined = "\n\n".join(texts)
    return f"{prompt}\n\n{joined}" if prompt else joined


def to_langchain_blocks(
    prompt: str,
    attachments: list[NormalizedAttachment],
) -> list[dict[str, Any]]:
    """Build a LangChain multimodal content-block list for a human message.

    Images become ``image_url`` blocks (broadly supported across providers).
    Text documents are decoded and inlined as labelled text. Other documents
    (e.g. PDF) become ``file`` blocks for providers that accept them.
    """
    blocks: list[dict[str, Any]] = []
    if prompt:
        blocks.append({"type": "text", "text": prompt})

    for a in attachments:
        mime = a["mime_type"]
        if a["kind"] == "image":
            blocks.append({"type": "image_url", "image_url": {"url": data_url(a)}})
        elif mime in TEXT_DOCUMENT_MIME_TYPES:
            blocks.append({"type": "text", "text": _inline_text_document(a)})
        else:
            blocks.append(
                {
                    "type": "file",
                    "source_type": "base64",
                    "mime_type": mime,
                    "data": a["data"],
                    "filename": a.get("name"),
                }
            )
    return blocks


def _inline_text_document(attachment: NormalizedAttachment) -> str:
    """Decode a text document and wrap it with a filename header."""
    try:
        text = base64.b64decode(attachment["data"]).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        logger.warning("Failed to decode text attachment %s", attachment.get("name"))
        text = ""
    name = attachment.get("name") or "document"
    return f"[Attached file: {name}]\n\n{text}"
