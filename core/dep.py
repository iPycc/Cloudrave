

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.audit_logs import AuditLogRepository
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.objects import ObjectRepository
from repositories.passkeys import PasskeyRepository
from repositories.policies import StoragePolicyRepository
from repositories.sessions import SessionRepository
from repositories.shared import SharedRepository
from repositories.shares import ShareRepository
from repositories.site_settings import SiteSettingsRepository
from repositories.uploads import UploadRepository
from repositories.users import UserRepository
from security.jwt import TokenService
from security.password import PasswordService
from store.manager import StorageManager
from services.explorer.gc_job import GcWorker
from core.cfg import CloudraveConfig
from db.base import create_schema, make_async_engine, make_session_factory, session_scope, write_schema_sql
from core.log import LogOperation, LogOutcome, get_logger


LOGGER = get_logger("cloudrave.bootstrap")


class Dep:
    def __init__(self, config: CloudraveConfig) -> None:
        self.config = config
        self.engine = make_async_engine(config)
        self.session_factory = make_session_factory(self.engine)
        self.password_service = PasswordService(config.auth.password_salt)
        self.token_service = TokenService(config.auth)
        self.storage_manager = StorageManager(config=config, local_root=config.local_upload_root_path)
        self.gc_worker = GcWorker(self.session_factory, self.storage_manager)

    @property
    def share_signing_key(self) -> bytes:
        key = self.config.auth.share_signing_key
        if not key:
            raise RuntimeError("Missing [auth].share_signing_key in config.ini")
        return key.encode()

    async def check_database(self) -> None:
        """Verify database availability without running application bootstrap."""

        database_url = self._mask_database_url(self.config.database.url)
        LOGGER.info(
            "Checking %s database availability at %s.",
            self.config.database.backend.upper(),
            database_url,
        )
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (OSError, SQLAlchemyError) as exc:
            raise RuntimeError(
                "Cloudrave could not connect to the database. "
                f"Configured target: {database_url}. "
                f"Start {self.config.database.backend} or update [database] database/url in "
                f"{self.config.config_path}."
            ) from exc
        LOGGER.info(
            "Database connection check passed.",
            extra={
                "operation": LogOperation.DATABASE_CHECK,
                "resource": "cloudrave.db.base",
                "outcome": LogOutcome.SUCCESS,
            },
        )

    async def connect(self) -> None:
        schema_path = write_schema_sql(self.config)
        LOGGER.info(
            "Schema snapshot refreshed at %s.",
            schema_path,
        )

        try:
            await create_schema(self.engine)
        except (OSError, SQLAlchemyError) as exc:
            raise RuntimeError(
                "Cloudrave 无法连接数据库。"
                f" 当前配置: {self._mask_database_url(self.config.database.url)}。"
                f" 请先启动 {self.config.database.backend}，或修改 {self.config.config_path} 中 [database] 的 database/url 配置。"
            ) from exc

        LOGGER.info("Database schema and runtime migrations are ready.")

        async with self.session_scope() as session:
            from services.admin.ids import ObjectIdService

            filled, failed = await ObjectIdService(
                session=session,
                mounts=self.get_mount_repository(session),
                policies=self.get_policy_repository(session),
                objects=self.get_object_repository(session),
                storage_manager=self.storage_manager,
            ).fill()
            if filled or failed:
                LOGGER.info("Physical object identities backfilled: %s ready, %s unresolved.", filled, failed)

        async with self.session_scope() as session:
            from services.explorer.gc import ObjectGc

            cleaned = await ObjectGc(
                mounts=self.get_mount_repository(session),
                nodes=self.get_node_repository(session),
                policies=self.get_policy_repository(session),
                objects=self.get_object_repository(session),
                storage_manager=self.storage_manager,
            ).run_pending(limit=100)
            if cleaned:
                LOGGER.info("Retried %s pending physical object cleanup task(s).", cleaned)
        self.gc_worker.start()

        if len(self.config.auth.jwt_secret.strip()) < 32:
            LOGGER.warning(
                "JWT secret length is shorter than recommended. Please update [auth].jwt_secret in config.ini.",
                extra={"request_id": "bootstrap", "ip": "-", "actor": "system"},
            )

        async with self.session_scope() as session:
            from sqlalchemy import select

            from models.tables import SystemMeta

            admin_state = await session.scalar(
                select(SystemMeta).where(SystemMeta.key == "auth.first_admin_user_id")
            )
            if admin_state is None:
                session.add(SystemMeta(key="auth.first_admin_user_id", value=""))
                LOGGER.info("Initial administrator bootstrap marker created.")

    async def disconnect(self) -> None:
        await self.gc_worker.stop()
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

    def get_shared_repository(self, session: AsyncSession) -> SharedRepository:
        return SharedRepository(session)

    def get_site_settings_repository(self, session: AsyncSession) -> SiteSettingsRepository:
        return SiteSettingsRepository(session)

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
