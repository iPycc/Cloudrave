"""Public storage driver metadata and connection inspection."""

from typing import Any
from pydantic import Field, field_validator

from schemas.common import CloudraveSchema


class StorageDriverRead(CloudraveSchema):
    id: str
    label: str
    kind: str
    icon_key: str
    enabled: bool
    capabilities: list[str] = Field(default_factory=list)


class StorageDriverInspectRequest(CloudraveSchema):
    credentials: dict[str, str] = Field(exclude=True, repr=False)

    @field_validator("credentials")
    @classmethod
    def validate_credentials(cls, value: dict[str, str]) -> dict[str, str]:
        if not value or len(value) > 8:
            raise ValueError("credentials must contain between 1 and 8 fields")
        for key, item in value.items():
            if len(key) > 64 or len(item) > 65_536:
                raise ValueError("credential field is too large")
        return value


class StorageDriverInspectRead(CloudraveSchema):
    provider: str
    account_id: str
    display_name: str | None = None
    root_id: str
    used_bytes: int = Field(ge=0)
    total_bytes: int = Field(gt=0)
    free_bytes: int = Field(ge=0)
    extra: dict[str, Any] = Field(default_factory=dict)
