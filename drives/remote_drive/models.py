"""Provider-neutral records returned by remote drive protocol clients."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RemoteAccount:
    account_id: str
    display_name: str | None
    root_id: str
    used_bytes: int
    total_bytes: int

    @property
    def free_bytes(self) -> int:
        return max(0, self.total_bytes - self.used_bytes)


@dataclass(frozen=True, slots=True)
class RemoteEntry:
    remote_id: str
    parent_id: str
    name: str
    is_dir: bool
    size: int = 0
    modified_at: datetime | None = None
    download_id: str | None = None
