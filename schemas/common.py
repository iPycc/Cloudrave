"""Common API schemas."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, SerializerFunctionWrapHandler, field_serializer


def serialize_utc_datetime(value: datetime) -> str:
    """Serialize every API datetime as an unambiguous UTC RFC 3339 value.

    SQLite and MySQL can return naive values even for ``DateTime(timezone=True)``.
    Cloudrave stores those values as UTC, so restore that contract before JSON
    serialization instead of letting browsers interpret them as local time.
    """

    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


class CloudraveSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_serializer("*", mode="wrap", when_used="json", check_fields=False)
    def serialize_datetime_fields(self, value, handler: SerializerFunctionWrapHandler):
        if isinstance(value, datetime):
            return serialize_utc_datetime(value)
        return handler(value)


class MessageResponse(CloudraveSchema):
    message: str


class HealthResponse(CloudraveSchema):
    status: str
    service: str
    timestamp: datetime
