"""Image variants and Deep Zoom preview behavior."""

from __future__ import annotations

import asyncio
import math
import tempfile
from pathlib import Path

from fastapi import HTTPException, status
from PIL import Image, ImageOps

from models.tables import BucketMount, Node, StoragePolicy, User
from services.explorer.view.base import (
    IMAGE_VARIANTS,
    derivative_semaphore as _derivative_semaphore,
    lock_for as _lock_for,
    preview_kind,
    preview_version,
)


class ImageMixin:
    async def image_asset(self, actor: User, node_id: int, variant: str) -> tuple[Path, str]:
        if variant not in IMAGE_VARIANTS:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown image preview variant")
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        if preview_kind(node.name) != "image":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not an image")
        version = preview_version(node)
        target = self.cache_root / "images" / str(node.id) / version / f"{variant}.webp"
        if not target.exists():
            lock = await _lock_for(str(target))
            async with lock:
                if not target.exists():
                    async with _derivative_semaphore:
                        await asyncio.to_thread(self._generate_image_variant, node, mount, policy, target, IMAGE_VARIANTS[variant])
        return target, f"{Path(node.name).stem}-{variant}.webp"
    async def dzi_descriptor(self, actor: User, node_id: int) -> str:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        metadata = await self._cached_json(
            node,
            "image-metadata",
            lambda: self._probe_image(node, mount, policy),
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Image TileSize="256" Overlap="1" Format="webp" xmlns="http://schemas.microsoft.com/deepzoom/2008">'
            f'<Size Width="{metadata["width"]}" Height="{metadata["height"]}"/>'
            '</Image>'
        )

    async def dzi_tile(self, actor: User, node_id: int, level: int, tile_name: str) -> Path:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        if not tile_name.endswith(".webp") or "_" not in tile_name:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid image tile")
        try:
            column_text, row_text = tile_name.removesuffix(".webp").split("_", 1)
            column, row = int(column_text), int(row_text)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid image tile") from exc
        if min(level, column, row) < 0:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid image tile")
        version = preview_version(node)
        target = self.cache_root / "images" / str(node.id) / version / "dzi_files" / str(level) / tile_name
        if not target.exists():
            lock = await _lock_for(str(target))
            async with lock:
                if not target.exists():
                    async with _derivative_semaphore:
                        await asyncio.to_thread(self._generate_dzi_tile, node, mount, policy, target, level, column, row)
        return target
    def _probe_image(self, node: Node, mount: BucketMount, policy: StoragePolicy) -> dict[str, object]:
        with self._open_seekable(node, mount, policy) as stream:
            with Image.open(stream) as image:
                oriented = ImageOps.exif_transpose(image)
                return {
                    "width": oriented.width,
                    "height": oriented.height,
                    "format": image.format or "",
                    "mode": image.mode,
                    "frames": int(getattr(image, "n_frames", 1)),
                }

    def _generate_image_variant(
        self,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        target: Path,
        width: int,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._open_seekable(node, mount, policy) as stream:
            with Image.open(stream) as source:
                image = ImageOps.exif_transpose(source)
                image.thumbnail((width, width * 4), Image.Resampling.LANCZOS)
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA" if "transparency" in image.info else "RGB")
                with tempfile.NamedTemporaryFile(suffix=".webp", dir=target.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                try:
                    image.save(temporary, format="WEBP", quality=82, method=4)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)

    def _generate_dzi_tile(
        self,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        target: Path,
        level: int,
        column: int,
        row: int,
    ) -> None:
        tile_size, overlap = 256, 1
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._open_seekable(node, mount, policy) as stream:
            with Image.open(stream) as source:
                image = ImageOps.exif_transpose(source)
                max_level = math.ceil(math.log2(max(image.width, image.height)))
                if level > max_level:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image tile level is out of range")
                scale = 2 ** (max_level - level)
                scaled_width = math.ceil(image.width / scale)
                scaled_height = math.ceil(image.height / scale)
                left = max(column * tile_size - (overlap if column else 0), 0)
                top = max(row * tile_size - (overlap if row else 0), 0)
                right = min((column + 1) * tile_size + overlap, scaled_width)
                bottom = min((row + 1) * tile_size + overlap, scaled_height)
                if left >= right or top >= bottom:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image tile is out of range")
                original_box = (
                    min(left * scale, image.width),
                    min(top * scale, image.height),
                    min(right * scale, image.width),
                    min(bottom * scale, image.height),
                )
                tile = image.crop(original_box)
                tile.thumbnail((right - left, bottom - top), Image.Resampling.LANCZOS)
                if tile.mode not in {"RGB", "RGBA"}:
                    tile = tile.convert("RGB")
                with tempfile.NamedTemporaryFile(suffix=".webp", dir=target.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                try:
                    tile.save(temporary, format="WEBP", quality=84, method=4)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
