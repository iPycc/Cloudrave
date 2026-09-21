"""Preview manifests, derived assets, and high-throughput file responses."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import shutil
import tempfile
from io import BytesIO
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable
from urllib.parse import quote

from fastapi import HTTPException, Request, status
from mutagen import File as MutagenFile
from PIL import Image, UnidentifiedImageError

from models.tables import BucketMount, Node, StoragePolicy, User
from repositories.audit_logs import AuditLogRepository
from repositories.nodes import NodeRepository
from repositories.nodes import _unique_name
from repositories.objects import ObjectRepository
from repositories.preview_annotations import PreviewAnnotationRepository
from schemas.preview import (
    PdfAnnotationDocument,
    PdfAnnotationItem,
    PreviewAssetRead,
    PreviewManifestRead,
    TextContentUpdateRead,
)
from security.jwt import AuthError, TokenService
from security.capabilities import Capability, has_capability, require_capability
from services.explorer.download import DownloadFileService
from services.explorer.view.arc import ARCHIVE_READ_ERRORS, ArcMixin
from services.explorer.view.base import (
    DERIVED_CACHE_CONTROL,
    IMAGE_VARIANTS,
    OFFICE_SOURCE_PURPOSE,
    PREVIEW_CACHE_CONTROL,
    SEEKABLE_MAX_BYTES,
    build_local_preview_response,
    lock_for as _lock_for,
    normalize_etag as _normalize_etag,
    preview_kind,
    preview_version,
    relative_preview_url,
)
from services.explorer.view.doc import DocMixin
from services.explorer.view.img import ImageMixin
from services.explorer.view.media import (
    MEDIA_MARKER_STALE_SECONDS,
    MediaMixin,
    media_job_state,
)
from store.manager import StorageManager
from store.call import call_adapter
from store.errors import is_not_found
from store.key import managed_key


class PreviewService(ImageMixin, DocMixin, MediaMixin, ArcMixin):
    """Resolve files once, then expose a renderer-oriented preview contract."""

    def __init__(
        self,
        *,
        resolver: DownloadFileService,
        nodes: NodeRepository,
        objects: ObjectRepository,
        audits: AuditLogRepository,
        annotations: PreviewAnnotationRepository,
        storage_manager: StorageManager,
        token_service: TokenService,
        cache_root: Path,
        public_base_url: str,
        office_ttl_seconds: int,
        text_max_bytes: int,
    ) -> None:
        self.resolver = resolver
        self.nodes = nodes
        self.objects = objects
        self.audits = audits
        self.annotations = annotations
        self.storage_manager = storage_manager
        self.token_service = token_service
        self.cache_root = cache_root
        self.public_base_url = public_base_url
        self.office_ttl_seconds = office_ttl_seconds
        self.text_max_bytes = text_max_bytes

    @staticmethod
    def _mutagen_file(*args: Any, **kwargs: Any) -> Any:
        """Keep the historical preview-module injection seam."""

        return MutagenFile(*args, **kwargs)

    async def manifest(self, request: Request, actor: User, node_id: int) -> PreviewManifestRead:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        manifest = await self._build_manifest(request, node, mount, policy)
        if Path(node.name).suffix.lower() in {".zip", ".7z"} and manifest.status == "ready":
            manifest.capabilities.append("extract")
        if manifest.kind == "pdf" and manifest.status == "ready":
            manifest.capabilities.append("annotate-personal")
        if not has_capability(actor, Capability.FILE_EDIT):
            manifest.capabilities = [item for item in manifest.capabilities if item != "edit"]
        if not has_capability(actor, Capability.TASK_MANAGE):
            manifest.capabilities = [item for item in manifest.capabilities if item != "extract"]
        return manifest

    async def shared_manifest(
        self,
        request: Request,
        node_id: int,
        *,
        source_url: str,
        archive_entry_url: str | None = None,
        cover_url: str | None = None,
    ) -> PreviewManifestRead:
        node, mount, policy = await self.resolver.resolve_shared(node_id)
        manifest = await self._build_manifest(request, node, mount, policy)
        source_asset = manifest.assets["source"]
        if source_asset.delivery == "storage":
            assets = {
                "source": source_asset,
                "proxy": PreviewAssetRead(
                    url=source_url,
                    mime_type=source_asset.mime_type,
                    size=source_asset.size,
                    supports_range=source_asset.supports_range,
                ),
            }
        else:
            assets = {
                "source": source_asset.model_copy(update={"url": source_url}),
            }
        if manifest.kind == "office":
            for name in ("office_source", "office_viewer"):
                if name in manifest.assets:
                    assets[name] = manifest.assets[name]
        if cover_url is not None and "cover" in manifest.assets:
            assets["cover"] = manifest.assets["cover"].model_copy(update={"url": cover_url})
        if manifest.kind == "archive" and archive_entry_url is not None:
            assets["archive_entry"] = PreviewAssetRead(
                url=archive_entry_url,
                mime_type="application/octet-stream",
            )
        return manifest.model_copy(
            update={
                "assets": assets,
                "capabilities": [
                    capability
                    for capability in manifest.capabilities
                    if capability not in {"edit", "extract", "annotate-personal"}
                ],
            }
        )

    async def _build_manifest(
        self,
        request: Request,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
    ) -> PreviewManifestRead:
        record = await self.objects.get_by_node_id(node.id)
        version = preview_version(node)
        kind = preview_kind(node.name)
        mime_type = (record.content_type if record else None) or mimetypes.guess_type(node.name)[0] or "application/octet-stream"
        assets = await self._source_assets(request, node, mount, policy, version, mime_type)
        metadata: dict[str, object] = {
            "extension": Path(node.name).suffix.lower().lstrip("."),
            "source_delivery": assets["source"].delivery,
        }
        capabilities = ["download", "properties"]
        manifest_status: str = "ready"
        error: str | None = None
        requires_preparation = False
        preparation_available = shutil.which("ffmpeg") is not None

        if kind == "image":
            capabilities.extend(["zoom", "pan", "rotate", "actual-size", "edit"])
            try:
                image_metadata = await self._cached_json(
                    node,
                    "image-metadata",
                    lambda: self._probe_image(node, mount, policy),
                )
                metadata.update(image_metadata)
                for variant, width in IMAGE_VARIANTS.items():
                    assets[variant] = PreviewAssetRead(
                        url=relative_preview_url(
                            request.url_for("preview_image_asset", node_id=node.id, variant=variant).include_query_params(v=version)
                        ),
                        mime_type="image/webp",
                        width=min(width, int(image_metadata["width"])),
                    )
                if max(int(image_metadata["width"]), int(image_metadata["height"])) > 4096:
                    assets["dzi"] = PreviewAssetRead(
                        url=relative_preview_url(request.url_for("preview_dzi", node_id=node.id).include_query_params(v=version)),
                        mime_type="application/xml",
                    )
                    metadata["tiled"] = True
            except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
                metadata["probe_error"] = "Image metadata unavailable"
                error = str(exc)
                if metadata["extension"] == "svg":
                    error = None
        elif kind in {"video", "audio"}:
            capabilities.extend(["play", "seek"])
            media_dir = self._media_dir(node, version)
            media_metadata = await self._cached_json(
                node,
                "audio-metadata-v2" if kind == "audio" else "media-metadata",
                lambda: self._probe_audio(node, mount, policy, media_dir / "cover.webp")
                if kind == "audio"
                else self._probe_media(node, mount, policy),
            )
            metadata.update(media_metadata)
            job_key = self._job_key(node, version)
            job_state, job_error = media_job_state(job_key)
            processing_marker = media_dir / ".processing"
            marker_active = False
            if processing_marker.exists():
                try:
                    marker_active = datetime.now(UTC).timestamp() - processing_marker.stat().st_mtime < MEDIA_MARKER_STALE_SECONDS
                except FileNotFoundError:
                    marker_active = False
                if not marker_active:
                    processing_marker.unlink(missing_ok=True)
            if kind == "video" and (media_dir / "master.m3u8").exists():
                assets["hls"] = PreviewAssetRead(
                    url=relative_preview_url(
                        request.url_for("preview_media_asset", node_id=node.id, version=version, asset_path="master.m3u8")
                    ),
                    mime_type="application/vnd.apple.mpegurl",
                    supports_range=True,
                )
            elif kind == "audio" and (media_dir / "audio.mp3").exists():
                assets["audio"] = PreviewAssetRead(
                    url=relative_preview_url(
                        request.url_for("preview_media_asset", node_id=node.id, version=version, asset_path="audio.mp3")
                    ),
                    mime_type="audio/mpeg",
                    supports_range=True,
                )
            else:
                requires_preparation = not self._browser_compatible(kind, node.name, media_metadata)
            if kind == "audio" and (media_dir / "cover.webp").exists():
                assets["cover"] = PreviewAssetRead(
                    url=relative_preview_url(
                        request.url_for(
                            "preview_media_asset",
                            node_id=node.id,
                            version=version,
                            asset_path="cover.webp",
                        )
                    ),
                    mime_type="image/webp",
                    supports_range=True,
                )
            if kind == "video" and preparation_available:
                assets["poster"] = PreviewAssetRead(
                    url=relative_preview_url(
                        request.url_for("preview_video_poster", node_id=node.id).include_query_params(v=version)
                    ),
                    mime_type="image/webp",
                )
            if requires_preparation and not preparation_available:
                manifest_status = "unsupported"
                error = "FFmpeg is required to preview this media format"
            if job_state == "processing" or marker_active:
                manifest_status = "processing"
            elif job_state == "failed":
                manifest_status = "failed"
                error = job_error or "Media preparation failed"
        elif kind == "pdf":
            capabilities.extend(["pages", "search", "zoom", "print"])
        elif kind == "office":
            capabilities.append("microsoft-viewer")
            if assets["source"].delivery == "storage":
                office_source = assets["source"].url
                assets["office_source"] = assets["source"].model_copy()
            else:
                office_source = self._office_source_url(request, node, mount, policy, version)
                assets["office_source"] = PreviewAssetRead(
                    url=office_source,
                    mime_type=mime_type,
                    size=node.size,
                    supports_range=True,
                )
            assets["office_viewer"] = PreviewAssetRead(
                url=f"https://view.officeapps.live.com/op/embed.aspx?src={quote(office_source, safe='')}",
                mime_type="text/html",
                delivery="external",
            )
            metadata["external_service"] = "Microsoft Office Web Viewer"
            metadata["requires_public_https"] = True
            metadata["public_source_ready"] = office_source.startswith("https://")
        elif kind == "text":
            capabilities.append("syntax")
            editable = node.size <= self.text_max_bytes
            if editable:
                capabilities.append("edit")
            metadata["editable"] = editable
            metadata["max_bytes"] = self.text_max_bytes
        elif kind == "archive":
            capabilities.append("list-entries")
            try:
                entries = await self._cached_json(
                    node,
                    "archive-entries-v2",
                    lambda: self._archive_entries(node, mount, policy),
                )
                metadata["entries"] = entries[:200]
                metadata["entry_count"] = len(entries)
                metadata["entries_truncated"] = len(entries) > 200
                metadata["entry_count_capped"] = len(entries) >= 10_001
            except ARCHIVE_READ_ERRORS as exc:
                manifest_status = "unsupported"
                error = str(exc)
        else:
            manifest_status = "unsupported"
            error = "No online renderer is available for this file type"

        return PreviewManifestRead(
            node_id=node.id,
            name=node.name,
            version=version,
            kind=kind,
            status=manifest_status,
            mime_type=mime_type,
            size=node.size,
            metadata=metadata,
            assets=assets,
            capabilities=capabilities,
            requires_preparation=requires_preparation,
            preparation_available=preparation_available,
            error=error,
        )

    async def get_pdf_annotations(self, actor: User, node_id: int) -> PdfAnnotationDocument:
        node, _mount, _policy = await self.resolver.resolve(actor, node_id)
        if preview_kind(node.name) != "pdf":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not a PDF")
        current_version = preview_version(node)
        row = await self.annotations.get(actor.id, node.id)
        if row is None:
            return PdfAnnotationDocument(
                source_version=current_version,
                revision=0,
                annotations=[],
                stale=False,
            )
        return PdfAnnotationDocument(
            source_version=row.source_version,
            revision=row.revision,
            annotations=[PdfAnnotationItem.model_validate(item) for item in row.payload],
            stale=row.source_version != current_version,
        )

    async def save_pdf_annotations(
        self,
        actor: User,
        node_id: int,
        *,
        source_version: str,
        annotations: list[PdfAnnotationItem],
        if_match: str | None,
    ) -> PdfAnnotationDocument:
        node, _mount, _policy = await self.resolver.resolve(actor, node_id)
        if preview_kind(node.name) != "pdf":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not a PDF")
        current_version = preview_version(node)
        if source_version != current_version:
            raise HTTPException(status_code=status.HTTP_412_PRECONDITION_FAILED, detail="PDF changed since annotations were opened")
        if if_match is None:
            raise HTTPException(status_code=status.HTTP_428_PRECONDITION_REQUIRED, detail="If-Match is required when saving annotations")
        try:
            expected_revision = int(_normalize_etag(if_match))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid annotation revision") from exc

        row = await self.annotations.get_for_update(actor.id, node.id)
        payload = [item.model_dump(mode="json") for item in annotations]
        if row is None:
            if expected_revision != 0:
                raise HTTPException(status_code=status.HTTP_412_PRECONDITION_FAILED, detail="Annotations changed since they were opened")
            row = await self.annotations.create(
                owner_id=actor.id,
                node_id=node.id,
                source_version=current_version,
                payload=payload,
            )
        else:
            if row.revision != expected_revision:
                raise HTTPException(status_code=status.HTTP_412_PRECONDITION_FAILED, detail="Annotations changed since they were opened")
            row.source_version = current_version
            row.payload = payload
            row.revision += 1
            await self.annotations.session.flush()

        await self.audits.create(
            actor_id=actor.id,
            action="pdf.annotate",
            target=f"node:{node.id}",
            detail=f"revision={row.revision};items={len(payload)}",
            ip=None,
            user_agent=None,
            request_id=None,
        )
        return PdfAnnotationDocument(
            source_version=row.source_version,
            revision=row.revision,
            annotations=[PdfAnnotationItem.model_validate(item) for item in row.payload],
            stale=False,
        )

    async def _source_assets(
        self,
        request: Request,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        version: str,
        mime_type: str,
    ) -> dict[str, PreviewAssetRead]:
        proxy_asset = PreviewAssetRead(
            url=relative_preview_url(
                request.url_for("preview_file", node_id=node.id).include_query_params(v=version)
            ),
            mime_type=mime_type,
            size=node.size,
            supports_range=True,
        )
        if self.storage_manager.local_path(policy, mount, node.blob_path) is not None:
            return {"source": proxy_asset}
        if not node.blob_path:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="File has no storage object",
            )

        adapter = self.storage_manager.get_adapter(policy)
        try:
            await call_adapter(
                adapter,
                "head_object",
                policy,
                mount,
                node.blob_path,
            )
        except Exception as exc:
            if is_not_found(exc):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="预览源文件在存储中不存在",
                ) from exc
            raise
        signed = await call_adapter(
            adapter,
            "build_download_url",
            policy,
            mount,
            node.blob_path,
            download_name=node.name,
            inline=True,
        )
        return {
            "source": PreviewAssetRead(
                url=signed.url,
                mime_type=mime_type,
                size=node.size,
                supports_range=True,
                delivery="storage",
                expires_at=signed.expires_at,
            ),
            "proxy": proxy_asset,
        }

    async def save_text(
        self,
        actor: User,
        node_id: int,
        *,
        content: str,
        if_match: str | None,
    ) -> TextContentUpdateRead:
        require_capability(actor, Capability.FILE_EDIT)
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        if mount.read_only:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bucket mount is read-only")
        if preview_kind(node.name) != "text":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not editable text")
        locked_node = await self.nodes.get_by_id_for_update(node.id)
        if locked_node is None or locked_node.deleted_at is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File node not found")
        node = locked_node
        current_version = preview_version(node)
        if not if_match:
            raise HTTPException(status_code=status.HTTP_428_PRECONDITION_REQUIRED, detail="If-Match is required when editing text")
        if _normalize_etag(if_match) != current_version:
            raise HTTPException(status_code=status.HTTP_412_PRECONDITION_FAILED, detail="File changed since it was opened")
        payload = content.encode("utf-8")
        if len(payload) > self.text_max_bytes:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Text file exceeds the online editing limit")
        record = await self.objects.get_by_node_id(node.id)
        content_type = (record.content_type if record else None) or "text/plain; charset=utf-8"
        adapter = self.storage_manager.get_adapter(policy)
        if not node.blob_path:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="File has no storage object")
        current_blob = node.blob_path
        current_physical = (record.physical_id if record else None) or adapter.physical_id(
            policy,
            mount,
            current_blob,
        )
        references = await self.objects.count_physical_references(
            physical_id=current_physical,
            policy_id=policy.id,
            mount_id=mount.id,
            blob_path=current_blob,
        )
        target_blob = (
            managed_key(
                actor.id,
                node.name,
                style=str((mount.extra or {}).get("object_key_style", "readable")),
            )
            if references > 1
            else current_blob
        )
        extra = dict(record.extra if record else {})
        if target_blob != current_blob:
            extra["origin"] = "managed_edit"
        await call_adapter(
            adapter,
            "put_object",
            policy,
            mount,
            target_blob,
            payload,
            mime_type=content_type,
            metadata=extra,
        )
        checksum = hashlib.sha256(payload).hexdigest()
        node.blob_path = target_blob
        await self.nodes.update_file_metadata(node, size=len(payload))
        await self.objects.create_or_update(
            node_id=node.id,
            policy_id=policy.id,
            blob_path=target_blob,
            physical_id=adapter.physical_id(policy, mount, target_blob),
            revive_physical=True,
            checksum=checksum,
            content_type=content_type,
            extra=extra,
        )
        await self.audits.create(
            actor_id=actor.id,
            action="file.edit",
            target=f"node:{node.id}",
            detail=f"version={preview_version(node)}",
            ip=None,
            user_agent=None,
            request_id=None,
        )
        return TextContentUpdateRead(version=preview_version(node), size=len(payload))

    async def save_image_as(
        self,
        actor: User,
        node_id: int,
        *,
        filename: str,
        payload: bytes,
        mime_type: str,
    ) -> Node:
        require_capability(actor, Capability.FILE_EDIT)
        source, mount, policy = await self.resolver.resolve(actor, node_id)
        if mount.read_only:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bucket mount is read-only")
        if preview_kind(source.name) != "image":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not an image")
        if len(payload) > 64 * 1024 * 1024:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Edited image exceeds 64 MB")
        if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Edited image format is not supported")
        safe_name = Path(filename).name.strip()
        if not safe_name or safe_name in {".", ".."} or safe_name != filename.strip():
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid output filename")
        expected_suffixes = {
            "image/png": {".png"},
            "image/jpeg": {".jpg", ".jpeg"},
            "image/webp": {".webp"},
        }[mime_type]
        if Path(safe_name).suffix.lower() not in expected_suffixes:
            suffix = ".png" if mime_type == "image/png" else sorted(expected_suffixes)[0]
            safe_name = f"{safe_name}{suffix}"
        if len(safe_name) > 255:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Output filename is too long")
        try:
            with Image.open(BytesIO(payload)) as image:
                image.verify()
        except (OSError, UnidentifiedImageError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Edited image payload is invalid") from exc

        existing = await self.nodes.get_child_names(source.mount_id, source.parent_id)
        target_name = _unique_name(safe_name, existing)
        adapter = self.storage_manager.get_adapter(policy)
        object_key = managed_key(
            actor.id,
            target_name,
            style=str((mount.extra or {}).get("object_key_style", "readable")),
        )
        stored = await call_adapter(
            adapter,
            "put_object",
            policy,
            mount,
            object_key,
            payload,
            mime_type=mime_type,
            metadata={"origin": "preview_image_edit", "source_node_id": str(source.id)},
        )
        created = await self.nodes.create_file(
            owner_id=actor.id,
            mount_id=source.mount_id,
            parent_id=source.parent_id,
            name=target_name,
            blob_path=object_key,
            size=len(payload),
        )
        await self.objects.create_or_update(
            node_id=created.id,
            policy_id=policy.id,
            blob_path=object_key,
            physical_id=adapter.physical_id(policy, mount, object_key),
            revive_physical=True,
            checksum=hashlib.sha256(payload).hexdigest(),
            content_type=mime_type,
            extra={
                "origin": "preview_image_edit",
                "source_node_id": source.id,
                "etag": getattr(stored, "etag", None),
            },
        )
        await self.audits.create(
            actor_id=actor.id,
            action="image.save_as",
            target=f"node:{created.id}",
            detail=f"source_node:{source.id}",
            ip=None,
            user_agent=None,
            request_id=None,
        )
        return created

    def validate_office_token(self, token: str, node: Node) -> None:
        try:
            payload = self.token_service.parse_scoped_token(token, OFFICE_SOURCE_PURPOSE)
        except AuthError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid office source token") from exc
        if (
            str(payload.get("sub", "")) != str(node.owner_id)
            or int(payload.get("node_id", 0)) != node.id
            or payload.get("version") != preview_version(node)
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Office source token no longer matches the file")

    def _office_source_url(
        self,
        request: Request,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        version: str,
    ) -> str:
        adapter = self.storage_manager.get_adapter(policy)
        if self.storage_manager.local_path(policy, mount, node.blob_path) is None:
            return adapter.build_download_url(policy, mount, node.blob_path, expires_in=self.office_ttl_seconds).url
        token = self.token_service.issue_scoped_token(
            sub=str(node.owner_id),
            purpose=OFFICE_SOURCE_PURPOSE,
            ttl_seconds=self.office_ttl_seconds,
            claims={"node_id": node.id, "version": version},
        )
        source_url = request.url_for("office_source", node_id=node.id).include_query_params(token=token)
        if self.public_base_url:
            return f"{self.public_base_url}{source_url.path}?{source_url.query}"
        return str(source_url)

    def _source_locator(self, node: Node, mount: BucketMount, policy: StoragePolicy, *, expires_in: int = 3600) -> str:
        adapter = self.storage_manager.get_adapter(policy)
        local_path = self.storage_manager.local_path(policy, mount, node.blob_path)
        if local_path is not None:
            return str(local_path)
        return adapter.build_download_url(policy, mount, node.blob_path, expires_in=expires_in).url

    def _open_seekable(self, node: Node, mount: BucketMount, policy: StoragePolicy) -> BinaryIO:
        if node.size > SEEKABLE_MAX_BYTES:
            raise OSError("File is too large for seekable server-side preview metadata")
        adapter = self.storage_manager.get_adapter(policy)
        stream = adapter.open_object(policy, mount, node.blob_path)
        if getattr(stream, "seekable", lambda: False)():
            return stream
        spool = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
        try:
            while chunk := stream.read(1024 * 1024):
                spool.write(chunk)
        finally:
            stream.close()
        spool.seek(0)
        return spool

    async def _cached_json(self, node: Node, category: str, loader: Callable[[], Any]) -> Any:
        target = self.cache_root / "metadata" / str(node.id) / preview_version(node) / f"{category}.json"
        try:
            return await asyncio.to_thread(self._read_json, target)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        lock = await _lock_for(str(target))
        async with lock:
            try:
                return await asyncio.to_thread(self._read_json, target)
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                value = await asyncio.to_thread(loader)
                await asyncio.to_thread(self._write_json_atomic, target, value)
                return value

    @staticmethod
    def _read_json(path: Path) -> Any:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _write_json_atomic(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            temporary = Path(handle.name)
        temporary.replace(path)
