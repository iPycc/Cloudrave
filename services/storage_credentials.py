"""One-way migration of legacy plaintext storage credentials."""

from __future__ import annotations

from configparser import ConfigParser
from datetime import UTC, datetime
from pathlib import Path
import shutil
import sqlite3

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.cfg import CloudraveConfig, _serialize_config
from drives.ids import TENCENT_COS, provider_id
from models.tables import StoragePolicy
from store.manager import StorageManager


async def migrate_storage_credentials(
    session: AsyncSession,
    *,
    config: CloudraveConfig,
    storage_manager: StorageManager,
) -> int:
    policies = list(await session.scalars(select(StoragePolicy).order_by(StoragePolicy.id.asc())))
    for policy in policies:
        if policy.credential_ciphertext:
            if int(policy.credential_version or 0) != storage_manager.vault.VERSION:
                raise RuntimeError(
                    f"Unsupported credential payload version for storage policy {policy.id}"
                )
            # Fail startup on a wrong key. Never silently replace encrypted data.
            storage_manager.credentials_for(policy)

    candidates = [policy for policy in policies if _legacy_credentials(policy, config)]
    if not candidates:
        return 0

    _backup_runtime_files(config)
    migrated = 0
    for policy in candidates:
        legacy = _legacy_credentials(policy, config)
        existing = storage_manager.credentials_for(policy)
        merged = {**legacy, **existing}
        definition = storage_manager.definition_for(policy.provider)
        validated = definition.validate_credentials(
            merged,
            require=bool(definition.credential_fields),
        )
        policy.credential_ciphertext = storage_manager.vault.seal(validated) if validated else None
        policy.credential_version = storage_manager.vault.VERSION
        policy.secret_id = None
        policy.secret_key = None
        policy.session_token = None
        migrated += 1
    # Commit the encrypted database state before clearing config.ini. If the
    # config write fails, the legacy values remain available for recovery;
    # plaintext is never removed before the durable encrypted copy exists.
    await session.commit()

    _clear_legacy_config(config.config_path)
    config.tencent_cos.secret_id = ""
    config.tencent_cos.secret_key = ""
    return migrated


def _legacy_credentials(policy: StoragePolicy, config: CloudraveConfig) -> dict[str, str]:
    if provider_id(policy.provider) != TENCENT_COS:
        return {}
    values = {
        "secret_id": str(policy.secret_id or config.tencent_cos.secret_id or "").strip(),
        "secret_key": str(policy.secret_key or config.tencent_cos.secret_key or "").strip(),
        "session_token": str(policy.session_token or "").strip(),
    }
    return {key: value for key, value in values.items() if value}


def _backup_runtime_files(config: CloudraveConfig) -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    config_backup = config.config_path.with_name(f"{config.config_path.name}.before-drive-credentials-{stamp}.bak")
    shutil.copy2(config.config_path, config_backup)
    if config.database.backend != "sqlite":
        return
    source = Path(config.database.sqlite_path)
    if not source.is_absolute():
        source = (config.root_dir / source).resolve()
    if not source.exists():
        return
    target = source.with_name(f"{source.name}.before-drive-credentials-{stamp}.bak")
    with sqlite3.connect(source) as source_db, sqlite3.connect(target) as target_db:
        source_db.backup(target_db)


def _clear_legacy_config(path: Path) -> None:
    parser = ConfigParser()
    parser.read(path, encoding="utf-8")
    if not parser.has_section("tencent_cos"):
        return
    changed = False
    for field in ("secret_id", "secret_key"):
        if parser.get("tencent_cos", field, fallback=""):
            parser["tencent_cos"][field] = ""
            changed = True
    if changed:
        path.write_text(_serialize_config(parser), encoding="utf-8")
