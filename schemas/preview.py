"""Online preview schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from schemas.common import CloudraveSchema


PreviewKind = Literal["image", "video", "audio", "pdf", "office", "text", "archive", "unsupported"]
PreviewStatus = Literal["ready", "processing", "unsupported", "failed"]
PreviewDelivery = Literal["cloudrave", "storage", "external"]
OfficeCardKind = Literal["document", "spreadsheet", "presentation", "unsupported"]
OfficeCoverKind = Literal["thumbnail", "slide-image"]


class PreviewAssetRead(CloudraveSchema):
    url: str
    mime_type: str
    width: int | None = None
    height: int | None = None
    size: int | None = None
    supports_range: bool = False
    delivery: PreviewDelivery = "cloudrave"
    expires_at: datetime | None = None


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


class OfficeCardPreviewRead(CloudraveSchema):
    kind: OfficeCardKind
    lines: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    cover_available: bool = False
    cover_kind: OfficeCoverKind | None = None
    aspect_ratio: float | None = None


class ArchiveEntryRead(CloudraveSchema):
    name: str
    size: int = 0
    compressed_size: int = 0
    directory: bool = False


class ArchiveEntryPageRead(CloudraveSchema):
    items: list[ArchiveEntryRead] = Field(default_factory=list)
    total: int = 0
    has_more: bool = False


class TextContentUpdate(CloudraveSchema):
    content: str
    encoding: Literal["utf-8"] = "utf-8"


class TextContentUpdateRead(CloudraveSchema):
    version: str
    size: int


class PdfAnnotationItem(CloudraveSchema):
    id: str = Field(min_length=1, max_length=64)
    page: int = Field(ge=1)
    kind: Literal["highlight", "pen", "text", "rectangle"]
    color: str = Field(default="#f59e0b", pattern=r"^#[0-9a-fA-F]{6}$")
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(default=0, ge=0, le=1)
    height: float = Field(default=0, ge=0, le=1)
    points: list[list[float]] = Field(default_factory=list, max_length=4096)
    text: str | None = Field(default=None, max_length=2000)
    rotation: Literal[0, 90, 180, 270] = 0

    @field_validator("points")
    @classmethod
    def validate_points(cls, value: list[list[float]]) -> list[list[float]]:
        for point in value:
            if len(point) != 2 or any(coordinate < 0 or coordinate > 1 for coordinate in point):
                raise ValueError("annotation points must contain normalized x/y pairs")
        return value


class PdfAnnotationUpdate(CloudraveSchema):
    source_version: str = Field(min_length=1, max_length=64)
    annotations: list[PdfAnnotationItem] = Field(default_factory=list, max_length=2000)

    @field_validator("annotations")
    @classmethod
    def validate_total_points(cls, value: list[PdfAnnotationItem]) -> list[PdfAnnotationItem]:
        if sum(len(item.points) for item in value) > 20_000:
            raise ValueError("annotation document contains too many pen points")
        return value


class PdfAnnotationDocument(CloudraveSchema):
    source_version: str
    revision: int = Field(ge=0)
    annotations: list[PdfAnnotationItem] = Field(default_factory=list)
    stale: bool = False
