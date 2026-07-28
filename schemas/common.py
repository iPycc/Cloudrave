"""Common API schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CloudraveSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MessageResponse(CloudraveSchema):
    message: str


class HealthResponse(CloudraveSchema):
    status: str
    service: str
    timestamp: datetime
