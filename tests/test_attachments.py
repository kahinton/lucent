"""Unit tests for multimodal attachment handling (``lucent.llm.attachments``)."""

from __future__ import annotations

import base64

import pytest

from lucent.llm import attachments as att


# 1x1 transparent PNG.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode("ascii")


def _image_payload(data: str | None = None, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "mime_type": "image/png",
        "name": "pixel.png",
        "data": data if data is not None else _PNG_B64,
    }
    payload.update(overrides)
    return payload


def _text_payload(text: str = "hello world", mime: str = "text/plain") -> dict[str, object]:
    return {
        "mime_type": mime,
        "name": "note.txt",
        "data": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }


# ── normalize_attachment ──────────────────────────────────────────────────
def test_normalize_image_returns_canonical_shape():
    result = att.normalize_attachment(_image_payload())
    assert result["kind"] == "image"
    assert result["mime_type"] == "image/png"
    assert result["name"] == "pixel.png"
    assert result["size"] == len(_PNG_BYTES)
    # Canonical base64 round-trips back to the original bytes.
    assert base64.b64decode(result["data"]) == _PNG_BYTES


def test_normalize_accepts_data_url_and_infers_mime():
    payload = {"data": f"data:image/png;base64,{_PNG_B64}", "name": "x.png"}
    result = att.normalize_attachment(payload)
    assert result["mime_type"] == "image/png"
    assert result["kind"] == "image"


def test_normalize_document_kind():
    result = att.normalize_attachment(_text_payload())
    assert result["kind"] == "document"
    assert result["mime_type"] == "text/plain"


def test_normalize_rejects_unsupported_mime():
    with pytest.raises(att.AttachmentError):
        att.normalize_attachment(_image_payload(mime_type="application/x-msdownload"))


def test_normalize_rejects_invalid_base64():
    with pytest.raises(att.AttachmentError):
        att.normalize_attachment(_image_payload(data="not!!base64"))


def test_normalize_rejects_missing_data():
    with pytest.raises(att.AttachmentError):
        att.normalize_attachment({"mime_type": "image/png", "data": ""})


def test_normalize_rejects_empty_payload():
    empty = base64.b64encode(b"").decode("ascii")
    with pytest.raises(att.AttachmentError):
        att.normalize_attachment(_image_payload(data=empty))


def test_normalize_enforces_image_size_limit(monkeypatch):
    monkeypatch.setattr(att, "MAX_IMAGE_BYTES", 4)
    with pytest.raises(att.AttachmentError):
        att.normalize_attachment(_image_payload())


def test_normalize_defaults_name_when_missing():
    result = att.normalize_attachment(_image_payload(name=""))
    assert result["name"] == "image.png"


# ── normalize_attachments (list) ──────────────────────────────────────────
def test_normalize_attachments_empty_inputs():
    assert att.normalize_attachments(None) == []
    assert att.normalize_attachments([]) == []


def test_normalize_attachments_rejects_non_list():
    with pytest.raises(att.AttachmentError):
        att.normalize_attachments({"data": _PNG_B64})


def test_normalize_attachments_enforces_count_limit(monkeypatch):
    monkeypatch.setattr(att, "MAX_ATTACHMENTS", 2)
    with pytest.raises(att.AttachmentError):
        att.normalize_attachments([_image_payload(), _image_payload(), _image_payload()])


# ── Engine conversions ────────────────────────────────────────────────────
def test_to_copilot_blobs_includes_images_excludes_text_docs():
    items = att.normalize_attachments([_image_payload(), _text_payload()])
    blobs = att.to_copilot_blobs(items)
    assert len(blobs) == 1
    assert blobs[0]["type"] == "blob"
    assert blobs[0]["mimeType"] == "image/png"


def test_to_copilot_blobs_keeps_pdf_as_blob():
    pdf = {
        "mime_type": "application/pdf",
        "name": "doc.pdf",
        "data": base64.b64encode(b"%PDF-1.4 fake").decode("ascii"),
    }
    blobs = att.to_copilot_blobs(att.normalize_attachments([pdf]))
    assert len(blobs) == 1
    assert blobs[0]["mimeType"] == "application/pdf"


def test_inline_text_documents_appends_decoded_text():
    items = att.normalize_attachments([_text_payload("secret phrase")])
    prompt = att.inline_text_documents("Question?", items)
    assert "Question?" in prompt
    assert "secret phrase" in prompt
    assert "note.txt" in prompt


def test_inline_text_documents_ignores_images():
    items = att.normalize_attachments([_image_payload()])
    assert att.inline_text_documents("hi", items) == "hi"


def test_to_langchain_blocks_image_and_text():
    items = att.normalize_attachments([_image_payload(), _text_payload("inline me")])
    blocks = att.to_langchain_blocks("Describe", items)
    types = [b["type"] for b in blocks]
    assert types[0] == "text"  # prompt first
    assert "image_url" in types
    image_block = next(b for b in blocks if b["type"] == "image_url")
    assert image_block["image_url"]["url"].startswith("data:image/png;base64,")
    assert any(b["type"] == "text" and "inline me" in b["text"] for b in blocks)


def test_to_langchain_blocks_pdf_as_file_block():
    pdf = {
        "mime_type": "application/pdf",
        "name": "doc.pdf",
        "data": base64.b64encode(b"%PDF-1.4 fake").decode("ascii"),
    }
    blocks = att.to_langchain_blocks("", att.normalize_attachments([pdf]))
    file_block = next(b for b in blocks if b.get("type") == "file")
    assert file_block["mime_type"] == "application/pdf"
    assert file_block["source_type"] == "base64"


# ── Storage seam ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_inline_store_round_trips():
    store = att.InlineAttachmentStore()
    items = att.normalize_attachments([_image_payload()])
    persisted = await store.persist(items)
    loaded = await store.load(persisted)
    assert loaded == items


@pytest.mark.asyncio
async def test_inline_store_load_drops_payloadless_entries():
    store = att.InlineAttachmentStore()
    loaded = await store.load([{"kind": "image", "mime_type": "image/png"}])
    assert loaded == []


def test_data_url_and_summary():
    item = att.normalize_attachment(_image_payload())
    assert att.data_url(item) == f"data:image/png;base64,{item['data']}"
    summary = att.attachment_summary([item])
    assert summary == [
        {"kind": "image", "mime_type": "image/png", "name": "pixel.png", "size": len(_PNG_BYTES)}
    ]
    # Summary never leaks the payload.
    assert "data" not in summary[0]
