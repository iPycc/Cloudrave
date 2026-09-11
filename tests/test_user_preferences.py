from __future__ import annotations

import pytest
from pydantic import ValidationError
from datetime import UTC, datetime
from sqlalchemy import create_engine, inspect, text

from models.enums import UserRole, UserStatus
from models.tables import User
from schemas.user import UserPreferencesUpdateRequest
from services.user.response import build_user_response
from db.mig.v2 import upgrade as upgrade_v2


def test_thumbnail_preference_is_exposed_in_user_response() -> None:
    user = User(
        id=1,
        uid="PREFS",
        email="prefs@example.test",
        username="prefs",
        password_hash="test",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
        timezone="Asia/Shanghai",
        thumbnails_enabled=False,
        created_at=datetime.now(UTC),
        password_updated_at=datetime.now(UTC),
        totp_enabled=False,
    )
    assert build_user_response(user).thumbnails_enabled is False


def test_preferences_patch_accepts_independent_thumbnail_update() -> None:
    payload = UserPreferencesUpdateRequest(thumbnails_enabled=False)
    assert payload.timezone is None
    assert payload.thumbnails_enabled is False

    with pytest.raises(ValidationError):
        UserPreferencesUpdateRequest()


def test_v2_migration_adds_thumbnail_preference_to_existing_users() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO users (id) VALUES (1)"))
        upgrade_v2(connection)
        columns = {column["name"] for column in inspect(connection).get_columns("users")}
        enabled = connection.scalar(text("SELECT thumbnails_enabled FROM users WHERE id = 1"))

    assert "thumbnails_enabled" in columns
    assert enabled == 1
