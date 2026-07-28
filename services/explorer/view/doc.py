"""Office document card extraction."""

from __future__ import annotations

from io import BytesIO
import posixpath
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from fastapi import HTTPException, status
from PIL import Image, ImageOps, UnidentifiedImageError

from models.tables import BucketMount, Node, StoragePolicy, User
from schemas.preview import OfficeCardPreviewRead
from services.explorer.view.base import (
    OFFICE_CARD_MAX_BYTES,
    preview_kind,
    preview_version,
)


OFFICE_XML_MAX_BYTES = 8 * 1024 * 1024
OFFICE_IMAGE_MAX_BYTES = 24 * 1024 * 1024

_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


class DocMixin:
    async def office_card_preview(self, actor: User, node_id: int) -> OfficeCardPreviewRead:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        if preview_kind(node.name) != "office":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not an Office document")
        if node.size > OFFICE_CARD_MAX_BYTES:
            return OfficeCardPreviewRead(kind="unsupported")

        cover_target = self._office_dir(node, preview_version(node)) / "cover.webp"
        payload = await self._cached_json(
            node,
            "office-card-v2",
            lambda: self._extract_office_card(node, mount, policy, cover_target),
        )
        payload["cover_available"] = cover_target.is_file()
        if not payload["cover_available"]:
            payload["cover_kind"] = None
        return OfficeCardPreviewRead.model_validate(payload)

    async def office_card_cover(self, actor: User, node_id: int) -> Path | None:
        node, _mount, _policy = await self.resolver.resolve(actor, node_id)
        if preview_kind(node.name) != "office":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not an Office document")
        preview = await self.office_card_preview(actor, node_id)
        if not preview.cover_available:
            return None
        target = self._office_dir(node, preview_version(node)) / "cover.webp"
        return target if target.is_file() else None
    def _extract_office_card(
        self,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        cover_target: Path,
    ) -> dict[str, object]:
        extension = Path(node.name).suffix.lower().lstrip(".")
        supported = {
            "docx": "document",
            "docm": "document",
            "dotx": "document",
            "xlsx": "spreadsheet",
            "xlsm": "spreadsheet",
            "xltx": "spreadsheet",
            "pptx": "presentation",
            "pptm": "presentation",
            "ppsx": "presentation",
        }
        kind = supported.get(extension)
        if kind is None:
            return {"kind": "unsupported"}

        try:
            with self._open_seekable(node, mount, policy) as stream, zipfile.ZipFile(stream) as archive:
                if kind == "document":
                    return self._word_card(archive)
                if kind == "spreadsheet":
                    return self._spreadsheet_card(archive)
                return self._presentation_card(archive, cover_target)
        except (
            ET.ParseError,
            IndexError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            zipfile.BadZipFile,
        ):
            cover_target.unlink(missing_ok=True)
            return {"kind": "unsupported"}

    @staticmethod
    def _zip_part(archive: zipfile.ZipFile, name: str, *, max_bytes: int = OFFICE_XML_MAX_BYTES) -> bytes:
        normalized = name.replace("\\", "/").lstrip("/")
        info = archive.getinfo(normalized)
        if info.file_size > max_bytes:
            raise ValueError(f"Office preview part is too large: {normalized}")
        payload = archive.read(info)
        if b"<!DOCTYPE" in payload[:4096].upper() or b"<!ENTITY" in payload[:4096].upper():
            raise ValueError("Office preview XML contains a disallowed document type")
        return payload

    @classmethod
    def _xml_part(cls, archive: zipfile.ZipFile, name: str) -> ET.Element:
        return ET.fromstring(cls._zip_part(archive, name))

    @staticmethod
    def _relationship_part(source_part: str) -> str:
        directory, filename = posixpath.split(source_part)
        return posixpath.join(directory, "_rels", f"{filename}.rels")

    @staticmethod
    def _resolve_part(source_part: str, target: str) -> str:
        if target.startswith("/"):
            resolved = posixpath.normpath(target.lstrip("/"))
        else:
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))
        if resolved == ".." or resolved.startswith("../"):
            raise ValueError("Office relationship leaves the package root")
        return resolved

    @classmethod
    def _relationships(cls, archive: zipfile.ZipFile, source_part: str) -> dict[str, str]:
        root = cls._xml_part(archive, cls._relationship_part(source_part))
        relationships: dict[str, str] = {}
        for relationship in root.findall(f"{{{_REL_NS}}}Relationship"):
            relationship_id = relationship.get("Id")
            target = relationship.get("Target")
            if not relationship_id or not target or relationship.get("TargetMode") == "External":
                continue
            relationships[relationship_id] = cls._resolve_part(source_part, target)
        return relationships

    @staticmethod
    def _clean_preview_text(value: str, *, limit: int = 240) -> str:
        return re.sub(r"\s+", " ", value).strip()[:limit]

    @classmethod
    def _word_card(cls, archive: zipfile.ZipFile) -> dict[str, object]:
        root = cls._xml_part(archive, "word/document.xml")
        namespace = {"w": _WORD_NS}
        body = root.find("w:body", namespace)
        lines: list[str] = []
        if body is None:
            return {"kind": "document", "lines": lines}

        for block in body:
            if block.tag == f"{{{_WORD_NS}}}p":
                value = cls._clean_preview_text("".join(text.text or "" for text in block.findall(".//w:t", namespace)))
                if value:
                    lines.append(value)
            elif block.tag == f"{{{_WORD_NS}}}tbl":
                for row in block.findall("w:tr", namespace):
                    cells = [
                        cls._clean_preview_text("".join(text.text or "" for text in cell.findall(".//w:t", namespace)), limit=80)
                        for cell in row.findall("w:tc", namespace)
                    ]
                    value = "  |  ".join(cell for cell in cells if cell)
                    if value:
                        lines.append(value)
            if len(lines) >= 40:
                break
        return {"kind": "document", "lines": lines}

    @classmethod
    def _spreadsheet_card(cls, archive: zipfile.ZipFile) -> dict[str, object]:
        namespace = {"x": _SHEET_NS, "r": _DOC_REL_NS}
        workbook_part = "xl/workbook.xml"
        workbook = cls._xml_part(archive, workbook_part)
        relationships = cls._relationships(archive, workbook_part)
        sheet_part: str | None = None
        for sheet in workbook.findall(".//x:sheets/x:sheet", namespace):
            if sheet.get("state", "visible") == "hidden":
                continue
            relationship_id = sheet.get(f"{{{_DOC_REL_NS}}}id")
            if relationship_id and relationship_id in relationships:
                sheet_part = relationships[relationship_id]
                break
        if sheet_part is None:
            return {"kind": "spreadsheet", "rows": []}

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = cls._xml_part(archive, "xl/sharedStrings.xml")
            for item in shared_root.findall("x:si", namespace):
                shared_strings.append(cls._clean_preview_text("".join(text.text or "" for text in item.findall(".//x:t", namespace))))

        sheet_root = cls._xml_part(archive, sheet_part)
        values: dict[tuple[int, int], str] = {}
        for cell in sheet_root.findall(".//x:sheetData/x:row/x:c", namespace):
            reference = cell.get("r", "")
            match = re.match(r"^([A-Z]+)(\d+)$", reference)
            if match is None:
                continue
            column = 0
            for letter in match.group(1):
                column = column * 26 + ord(letter) - 64
            row = int(match.group(2))
            cell_type = cell.get("t")
            raw = cell.findtext("x:v", default="", namespaces=namespace)
            if cell_type == "s" and raw:
                index = int(raw)
                value = shared_strings[index] if 0 <= index < len(shared_strings) else ""
            elif cell_type == "inlineStr":
                value = "".join(text.text or "" for text in cell.findall(".//x:is/x:t", namespace))
            elif cell_type == "b":
                value = "TRUE" if raw == "1" else "FALSE"
            else:
                value = raw
            value = cls._clean_preview_text(value, limit=80)
            if value:
                values[(row, column)] = value
            if len(values) >= 400:
                break

        if not values:
            return {"kind": "spreadsheet", "rows": []}
        first_row = min(row for row, _column in values)
        first_column = min(column for _row, column in values)
        last_row = min(max(row for row, _column in values), first_row + 11)
        last_column = min(max(column for _row, column in values), first_column + 7)
        rows = [
            [values.get((row, column), "") for column in range(first_column, last_column + 1)]
            for row in range(first_row, last_row + 1)
        ]
        while rows and not any(rows[-1]):
            rows.pop()
        while rows and rows[0] and all(not row[-1] for row in rows):
            for row in rows:
                row.pop()
        return {"kind": "spreadsheet", "rows": rows}

    @classmethod
    def _presentation_card(cls, archive: zipfile.ZipFile, cover_target: Path) -> dict[str, object]:
        namespace = {"p": _PRESENTATION_NS, "a": _DRAWING_NS, "r": _DOC_REL_NS}
        presentation_part = "ppt/presentation.xml"
        presentation = cls._xml_part(archive, presentation_part)
        relationships = cls._relationships(archive, presentation_part)
        slide_id = presentation.find(".//p:sldIdLst/p:sldId", namespace)
        relationship_id = slide_id.get(f"{{{_DOC_REL_NS}}}id") if slide_id is not None else None
        slide_part = relationships.get(relationship_id or "", "ppt/slides/slide1.xml")
        slide = cls._xml_part(archive, slide_part)

        title_lines: list[str] = []
        body_lines: list[str] = []
        for shape in slide.findall(".//p:sp", namespace):
            paragraphs = [
                cls._clean_preview_text("".join(text.text or "" for text in paragraph.findall(".//a:t", namespace)))
                for paragraph in shape.findall(".//a:p", namespace)
            ]
            paragraphs = [line for line in paragraphs if line]
            placeholder = shape.find("./p:nvSpPr/p:nvPr/p:ph", namespace)
            placeholder_type = placeholder.get("type", "") if placeholder is not None else ""
            target = title_lines if placeholder_type in {"title", "ctrTitle"} else body_lines
            target.extend(paragraphs)
        lines = (title_lines + body_lines)[:20]

        aspect_ratio: float | None = None
        slide_size = presentation.find("./p:sldSz", namespace)
        if slide_size is not None:
            width = int(slide_size.get("cx", "0") or 0)
            height = int(slide_size.get("cy", "0") or 0)
            if width > 0 and height > 0:
                aspect_ratio = round(width / height, 4)

        cover_kind = cls._write_presentation_cover(archive, slide_part, slide, cover_target)
        return {
            "kind": "presentation",
            "lines": lines,
            "cover_available": cover_kind is not None,
            "cover_kind": cover_kind,
            "aspect_ratio": aspect_ratio,
        }

    @classmethod
    def _write_presentation_cover(
        cls,
        archive: zipfile.ZipFile,
        slide_part: str,
        slide: ET.Element,
        target: Path,
    ) -> str | None:
        names = {name.lower(): name for name in archive.namelist()}
        thumbnail_name = next(
            (
                names[name]
                for name in ("docprops/thumbnail.jpeg", "docprops/thumbnail.jpg", "docprops/thumbnail.png")
                if name in names
            ),
            None,
        )
        candidates: list[tuple[str, str]] = []
        if thumbnail_name is not None:
            candidates.append(("thumbnail", thumbnail_name))
        else:
            try:
                relationships = cls._relationships(archive, slide_part)
            except (KeyError, ET.ParseError, ValueError):
                relationships = {}
            image_parts = []
            for blip in slide.findall(f".//{{{_DRAWING_NS}}}blip"):
                relationship_id = blip.get(f"{{{_DOC_REL_NS}}}embed")
                part = relationships.get(relationship_id or "")
                if part and part not in image_parts:
                    image_parts.append(part)
            candidates.extend(("slide-image", part) for part in image_parts)

        best: tuple[int, str, Image.Image] | None = None
        for cover_kind, part in candidates:
            try:
                payload = cls._zip_part(archive, part, max_bytes=OFFICE_IMAGE_MAX_BYTES)
                with Image.open(BytesIO(payload)) as source:
                    image = ImageOps.exif_transpose(source).convert("RGB")
                    image.load()
                area = image.width * image.height
                if best is None or area > best[0] or cover_kind == "thumbnail":
                    best = (area, cover_kind, image)
                if cover_kind == "thumbnail":
                    break
            except (KeyError, OSError, UnidentifiedImageError, Image.DecompressionBombError, ValueError):
                continue
        if best is None:
            target.unlink(missing_ok=True)
            return None

        _area, cover_kind, image = best
        image.thumbnail((960, 540), Image.Resampling.LANCZOS)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", suffix=".webp", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            image.save(temporary, "WEBP", quality=86, method=6)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return cover_kind
    def _office_dir(self, node: Node, version: str) -> Path:
        return self.cache_root / "office" / str(node.id) / version
