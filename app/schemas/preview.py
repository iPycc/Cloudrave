"""Online preview schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.common import CloudraveSchema


PreviewKind = Literal["image", "video", "audio", "pdf", "office", "text", "archive", "unsupported"]
PreviewStatus = Literal["ready", "processing", "unsupported", "failed"]


class PreviewAssetRead(CloudraveSchema):
    url: str
    mime_type: str
    width: int | None = None
    height: int | None = None
    size: int | None = None
    supports_range: bool = False


class PreviewManifestRead(CloudraveSchema):
    node_id: int
    name: str
    version: str
    kind: PreviewKind
    status: PreviewStatus = "ready"
    mime_type: str
    size: int
    metadata: dict[str, object] = Field(default_factory=dict)
    assets: dict[str, PreviewAssetRead] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    requires_preparation: bool = False
    preparation_available: bool = False
    error: str | None = None


class PreviewPrepareRead(CloudraveSchema):
    status: PreviewStatus
    message: str


class TextContentUpdate(CloudraveSchema):
    content: str
    encoding: Literal["utf-8"] = "utf-8"


class TextContentUpdateRead(CloudraveSchema):
    version: str
    size: int
