"""Dependency container similar to Cloudrave's centralized dependency layer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.audit_logs import AuditLogRepository
from app.repositories.mounts import BucketMountRepository
from app.repositories.nodes import NodeRepository
from app.repositories.objects import ObjectRepository
from app.repositories.passkeys import PasskeyRepository
from app.repositories.policies import StoragePolicyRepository
from app.repositories.sessions import SessionRepository
from app.repositories.shares import ShareRepository
from app.repositories.uploads import UploadRepository
from app.repositories.users import UserRepository
from app.security.jwt import TokenService
from app.security.password import PasswordService
from app.storage.manager import StorageManager
from config import CloudraveConfig
from database import create_schema, make_async_engine, make_session_factory, session_scope, write_schema_sql
from logging_config import get_logger


LOGGER = get_logger("cloudrave.bootstrap")


class Dep:
    def __init__(self, config: CloudraveConfig) -> None:
        self.config = config
        self.engine = make_async_engine(config)
        self.session_factory = make_session_factory(self.engine)
        self.password_service = PasswordService(config.auth.password_salt)
        self.token_service = TokenService(config.auth)
        self.storage_manager = StorageManager(config=config, local_root=config.local_upload_root_path)

    @property
    def share_signing_key(self) -> bytes:
        key = self.config.auth.share_signing_key
        if not key:
            raise RuntimeError("Missing [auth].share_signing_key in config.ini")
        return key.encode()


    async def connect(self) -> None:
        schema_path = write_schema_sql(self.config)
        LOGGER.info(
            "Generated database schema SQL.",
            extra={"request_id": "bootstrap", "ip": "-", "actor": "system", "target": str(schema_path)},
        )

        try:
            await create_schema(self.engine)
        except (OSError, SQLAlchemyError) as exc:
            raise RuntimeError(
                "Cloudrave 无法连接数据库。"
                f" 当前配置: {self._mask_database_url(self.config.database.url)}。"
                f" 请先启动 {self.config.database.backend}，或修改 {self.config.config_path} 中 [database] 的 database/url 配置。"
            ) from exc

        if len(self.config.auth.jwt_secret.strip()) < 32:
            LOGGER.warning(
                "JWT secret length is shorter than recommended. Please update [auth].jwt_secret in config.ini.",
                extra={"request_id": "bootstrap", "ip": "-", "actor": "system"},
            )

        async with self.session_scope() as session:
            from sqlalchemy import select

            from app.models.tables import SystemMeta

            admin_state = await session.scalar(
                select(SystemMeta).where(SystemMeta.key == "auth.first_admin_user_id")
            )
            if admin_state is None:
                session.add(SystemMeta(key="auth.first_admin_user_id", value=""))

    async def disconnect(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session_scope(self) -> AsyncIterator[AsyncSession]:
        async with session_scope(self.session_factory) as session:
            yield session

    def get_user_repository(self, session: AsyncSession) -> UserRepository:
        return UserRepository(session)

    def get_session_repository(self, session: AsyncSession) -> SessionRepository:
        return SessionRepository(session)

    def get_audit_repository(self, session: AsyncSession) -> AuditLogRepository:
        return AuditLogRepository(session)

    def get_policy_repository(self, session: AsyncSession) -> StoragePolicyRepository:
        return StoragePolicyRepository(session)

    def get_mount_repository(self, session: AsyncSession) -> BucketMountRepository:
        return BucketMountRepository(session)

    def get_node_repository(self, session: AsyncSession) -> NodeRepository:
        return NodeRepository(session)

    def get_object_repository(self, session: AsyncSession) -> ObjectRepository:
        return ObjectRepository(session)

    def get_passkey_repository(self, session: AsyncSession) -> PasskeyRepository:
        return PasskeyRepository(session)

    def get_upload_repository(self, session: AsyncSession) -> UploadRepository:
        return UploadRepository(session)

    def get_share_repository(self, session: AsyncSession) -> ShareRepository:
        return ShareRepository(session)

    def _mask_database_url(self, value: str) -> str:
        parsed = urlsplit(value)
        if not parsed.netloc or "@" not in parsed.netloc:
            return value

        credentials, host = parsed.netloc.rsplit("@", 1)
        if ":" in credentials:
            username, _password = credentials.split(":", 1)
            masked_netloc = f"{username}:***@{host}"
        else:
            masked_netloc = f"***@{host}"
        return urlunsplit((parsed.scheme, masked_netloc, parsed.path, parsed.query, parsed.fragment))
