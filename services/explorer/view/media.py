"""Audio and video preview behavior."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from io import BytesIO
import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status
from mutagen import MutagenError
from PIL import Image, ImageOps, UnidentifiedImageError

from models.tables import BucketMount, Node, StoragePolicy, User
from schemas.preview import PreviewPrepareRead
from services.explorer.view.base import (
    derivative_semaphore as _derivative_semaphore,
    lock_for as _lock_for,
    preview_kind,
    preview_version,
)


MEDIA_MARKER_STALE_SECONDS = 120
_media_jobs: dict[str, tuple[str, str | None]] = {}
_media_tasks: set[asyncio.Task[None]] = set()
_media_job_semaphore = asyncio.Semaphore(2)


def media_job_state(key: str) -> tuple[str, str | None]:
    return _media_jobs.get(key, ("idle", None))


class MediaMixin:
    async def video_poster(self, actor: User, node_id: int) -> Path:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        if preview_kind(node.name) != "video":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not a video")
        if shutil.which("ffmpeg") is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="FFmpeg is not installed on the server")
        target = self._media_dir(node, preview_version(node)) / "poster.webp"
        if not target.exists():
            lock = await _lock_for(str(target))
            async with lock:
                if not target.exists():
                    async with _derivative_semaphore:
                        await asyncio.to_thread(self._generate_video_poster, node, mount, policy, target)
        return target

    async def audio_cover(self, actor: User, node_id: int) -> Path | None:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        if preview_kind(node.name) != "audio":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not audio")
        version = preview_version(node)
        cover_target = self._media_dir(node, version) / "cover.webp"
        if cover_target.exists():
            return cover_target
        lock = await _lock_for(str(cover_target))
        async with lock:
            if cover_target.exists():
                return cover_target
            try:
                metadata = await asyncio.to_thread(self._probe_audio, node, mount, policy, cover_target)
            except (MutagenError, OSError, ValueError, TypeError, UnidentifiedImageError, Image.DecompressionBombError):
                return None
            if metadata.get("cover_available") and cover_target.exists():
                return cover_target
        return None

    async def shared_audio_cover(self, node_id: int) -> Path | None:
        node, mount, policy = await self.resolver.resolve_shared(node_id)
        if preview_kind(node.name) != "audio":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not audio")
        version = preview_version(node)
        cover_target = self._media_dir(node, version) / "cover.webp"
        if cover_target.exists():
            return cover_target
        lock = await _lock_for(str(cover_target))
        async with lock:
            if cover_target.exists():
                return cover_target
            try:
                metadata = await asyncio.to_thread(self._probe_audio, node, mount, policy, cover_target)
            except (MutagenError, OSError, ValueError, TypeError, UnidentifiedImageError, Image.DecompressionBombError):
                return None
            if metadata.get("cover_available") and cover_target.exists():
                return cover_target
        return None

    async def prepare_media(self, actor: User, node_id: int) -> PreviewPrepareRead:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        kind = preview_kind(node.name)
        if kind not in {"video", "audio"}:
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not audio or video")
        if shutil.which("ffmpeg") is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="FFmpeg is not installed on the server")
        version = preview_version(node)
        job_key = self._job_key(node, version)
        state, message = _media_jobs.get(job_key, ("idle", None))
        if state == "processing":
            return PreviewPrepareRead(status="processing", message="Media preparation is already running")
        media_dir = self._media_dir(node, version)
        if (kind == "video" and (media_dir / "master.m3u8").exists()) or (kind == "audio" and (media_dir / "audio.mp3").exists()):
            return PreviewPrepareRead(status="ready", message="Media preview is ready")
        media_dir.mkdir(parents=True, exist_ok=True)
        processing_marker = media_dir / ".processing"
        try:
            marker_fd = os.open(processing_marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                marker_age = datetime.now(UTC).timestamp() - processing_marker.stat().st_mtime
            except FileNotFoundError:
                marker_age = MEDIA_MARKER_STALE_SECONDS
            if marker_age < MEDIA_MARKER_STALE_SECONDS:
                return PreviewPrepareRead(status="processing", message="Media preparation is already running")
            processing_marker.unlink(missing_ok=True)
            try:
                marker_fd = os.open(processing_marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return PreviewPrepareRead(status="processing", message="Media preparation is already running")
        with os.fdopen(marker_fd, "w", encoding="utf-8") as marker:
            marker.write(f"pid={os.getpid()}\nstarted={datetime.now(UTC).isoformat()}\n")
        _media_jobs[job_key] = ("processing", None)
        task = asyncio.create_task(self._transcode_media(job_key, kind, node, mount, policy, media_dir, processing_marker))
        _media_tasks.add(task)
        task.add_done_callback(_media_tasks.discard)
        return PreviewPrepareRead(status="processing", message=message or "Media preparation started")

    async def media_asset(self, actor: User, node_id: int, version: str, asset_path: str) -> Path:
        node, _mount, _policy = await self.resolver.resolve(actor, node_id)
        if version != preview_version(node):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media preview version is no longer current")
        root = self._media_dir(node, version).resolve()
        target = (root / asset_path).resolve()
        if target != root and root not in target.parents:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid media asset")
        if not target.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found")
        return target
    def _probe_media(self, node: Node, mount: BucketMount, policy: StoragePolicy) -> dict[str, object]:
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            return {"probe_available": False}
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", self._source_locator(node, mount, policy)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=20,
            )
            if not result.stdout:
                return {"probe_available": True, "probe_failed": True}
            payload = json.loads(result.stdout)
            if not isinstance(payload, dict):
                return {"probe_available": True, "probe_failed": True}
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError, ValueError):
            return {"probe_available": True, "probe_failed": True}
        metadata: dict[str, object] = {"probe_available": True}
        format_data = payload.get("format", {})
        if format_data.get("duration"):
            metadata["duration"] = round(float(format_data["duration"]), 3)
        if format_data.get("format_name"):
            metadata["container"] = format_data["format_name"]
        for stream in payload.get("streams", []):
            if stream.get("codec_type") == "video" and "video_codec" not in metadata:
                metadata["video_codec"] = stream.get("codec_name", "")
                metadata["width"] = int(stream.get("width", 0))
                metadata["height"] = int(stream.get("height", 0))
            if stream.get("codec_type") == "audio" and "audio_codec" not in metadata:
                metadata["audio_codec"] = stream.get("codec_name", "")
                metadata["sample_rate"] = int(stream.get("sample_rate", 0) or 0)
                metadata["channels"] = int(stream.get("channels", 0) or 0)
        return metadata

    @staticmethod
    def _easy_tag(tags: Any, key: str) -> str:
        if not tags:
            return ""
        value = tags.get(key)
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        return str(value).strip()

    @staticmethod
    def _embedded_cover(audio: Any) -> bytes | None:
        pictures = getattr(audio, "pictures", None)
        if pictures:
            data = getattr(pictures[0], "data", None)
            if isinstance(data, bytes):
                return data
        tags = getattr(audio, "tags", None)
        if tags is None:
            return None
        getall = getattr(tags, "getall", None)
        if callable(getall):
            frames = getall("APIC")
            if frames:
                data = getattr(frames[0], "data", None)
                if isinstance(data, bytes):
                    return data
        covers = tags.get("covr") if hasattr(tags, "get") else None
        if covers:
            candidate = covers[0] if isinstance(covers, (list, tuple)) else covers
            try:
                return bytes(candidate)
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _write_audio_cover(payload: bytes, target: Path) -> dict[str, object]:
        if not payload or len(payload) > 20 * 1024 * 1024:
            return {}
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(BytesIO(payload)) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            with tempfile.NamedTemporaryFile(suffix=".webp", dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
            try:
                image.save(temporary, format="WEBP", quality=84, method=4)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return {"cover_available": True, "cover_width": image.width, "cover_height": image.height}

    def _probe_audio(
        self,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        cover_target: Path,
    ) -> dict[str, object]:
        metadata = self._probe_media(node, mount, policy)
        try:
            with self._open_seekable(node, mount, policy) as stream:
                audio = self._mutagen_file(stream, easy=True)
                if audio is None:
                    return metadata
                tags = getattr(audio, "tags", None)
                for target_key, tag_key in (
                    ("title", "title"),
                    ("artist", "artist"),
                    ("album", "album"),
                    ("album_artist", "albumartist"),
                    ("date", "date"),
                    ("track", "tracknumber"),
                    ("disc", "discnumber"),
                    ("genre", "genre"),
                ):
                    value = self._easy_tag(tags, tag_key)
                    if value:
                        metadata[target_key] = value
                info = getattr(audio, "info", None)
                if info is not None:
                    for key in ("length", "bitrate", "sample_rate", "channels"):
                        value = getattr(info, key, None)
                        if value is None:
                            continue
                        target_key = "duration" if key == "length" else key
                        metadata[target_key] = round(float(value), 3) if key == "length" else int(value)
                stream.seek(0)
                raw_audio = self._mutagen_file(stream, easy=False)
                cover = self._embedded_cover(raw_audio)
                if cover:
                    metadata.update(self._write_audio_cover(cover, cover_target))
                else:
                    metadata["cover_available"] = False
        except (MutagenError, OSError, ValueError, TypeError, UnidentifiedImageError, Image.DecompressionBombError):
            metadata["tags_available"] = False
        else:
            metadata["tags_available"] = True
        return metadata

    def _generate_video_poster(
        self,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        target: Path,
    ) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise OSError("FFmpeg is not installed")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".webp", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    self._source_locator(node, mount, policy, expires_in=3600),
                    "-vf",
                    "thumbnail=100,scale='min(1280,iw)':-2",
                    "-frames:v",
                    "1",
                    "-an",
                    "-c:v",
                    "libwebp",
                    "-q:v",
                    "75",
                    str(temporary),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=120,
            )
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def _browser_compatible(self, kind: str, name: str, metadata: dict[str, object]) -> bool:
        extension = Path(name).suffix.lower().lstrip(".")
        if kind == "video":
            codec = str(metadata.get("video_codec", ""))
            audio_codec = str(metadata.get("audio_codec", ""))
            if extension in {"mp4", "m4v"}:
                return (not codec or codec == "h264") and (not audio_codec or audio_codec in {"aac", "mp3"})
            if extension == "webm":
                return (not codec or codec in {"vp8", "vp9"}) and (not audio_codec or audio_codec in {"opus", "vorbis"})
            return False
        return extension in {"mp3", "m4a", "aac", "wav", "ogg", "opus"}

    async def _transcode_media(
        self,
        job_key: str,
        kind: str,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        media_dir: Path,
        processing_marker: Path,
    ) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            _media_jobs[job_key] = ("failed", "FFmpeg is not installed")
            processing_marker.unlink(missing_ok=True)
            return
        work_dir = media_dir / "_transcode"
        heartbeat = asyncio.create_task(self._heartbeat_marker(processing_marker))
        try:
            async with _media_job_semaphore:
                try:
                    await asyncio.to_thread(shutil.rmtree, work_dir, True)
                    work_dir.mkdir(parents=True, exist_ok=True)
                    source = self._source_locator(node, mount, policy, expires_in=7200)
                    if kind == "video":
                        command = [
                            ffmpeg, "-y", "-i", source,
                            "-map", "0:v:0", "-map", "0:a:0?",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                            "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "128k",
                            "-hls_time", "6", "-hls_playlist_type", "vod",
                            "-hls_segment_filename", str(work_dir / "segment-%05d.ts"),
                            str(work_dir / "master.m3u8"),
                        ]
                    else:
                        command = [ffmpeg, "-y", "-i", source, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(work_dir / "audio.mp3")]
                    await asyncio.to_thread(
                        subprocess.run,
                        command,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=True,
                        timeout=7200,
                    )
                    for asset in work_dir.iterdir():
                        asset.replace(media_dir / asset.name)
                    _media_jobs[job_key] = ("ready", None)
                except (OSError, subprocess.SubprocessError) as exc:
                    _media_jobs[job_key] = ("failed", str(exc))
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await asyncio.to_thread(shutil.rmtree, work_dir, True)
            processing_marker.unlink(missing_ok=True)

    @staticmethod
    async def _heartbeat_marker(marker: Path) -> None:
        while True:
            marker.touch(exist_ok=True)
            await asyncio.sleep(30)
    def _media_dir(self, node: Node, version: str) -> Path:
        return self.cache_root / "media" / str(node.id) / version
    @staticmethod
    def _job_key(node: Node, version: str) -> str:
        return f"{node.id}:{version}"
