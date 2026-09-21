"""Public storage driver metadata."""

from pydantic import Field

from schemas.common import CloudraveSchema


class StorageDriverRead(CloudraveSchema):
    id: str
    label: str
    kind: str
    icon_key: str
    enabled: bool
    capabilities: list[str] = Field(default_factory=list)
