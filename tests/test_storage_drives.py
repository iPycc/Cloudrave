from __future__ import annotations

from configparser import ConfigParser
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from core.cfg import load_config
from db.base import create_schema, make_async_engine
from db.mig.v6 import upgrade as upgrade_v6
from db.mig.v7 import upgrade as upgrade_v7
from drives.credentials import CredentialVault
from drives.drive_wo import DriveWoClient
from drives.ids import provider_id
from drives.registry import default_drive_registry
from models.tables import StoragePolicy
from schemas.policy import StoragePolicyRead
from services.storage_credentials import migrate_storage_credentials
from store.manager import StorageManager
from store.oss.driver import AliyunOssAdapter
from store.oss.stream import OssStream


def test_builtin_drive_registry_exposes_independent_drivers() -> None:
    definitions = default_drive_registry().definitions
    assert [item.id for item in definitions] == [
        "local_fs",
        "tencent_cos",
        "aliyun_oss",
        "drive_139",
        "drive_189",
        "drive_wo",
    ]
    assert definitions[2].kind == "object_storage"
    assert definitions[2].credential_fields == ("access_key_id", "access_key_secret")


def test_drive_wo_normalizes_url_encoded_cookie_token() -> None:
    token = "bd1b68af-dab5-4201-a76d-e97797a0507e"
    encoded = f"%22{token}%22"
    assert DriveWoClient._extract_token(encoded) == token
    assert DriveWoClient._extract_token(f"WoCloud-Web-Token={encoded}; other=value") == token


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ("LOCAL_FS", "local_fs"),
        ("TENCENT_COS", "tencent_cos"),
        ("ALIYUN_OSS", "aliyun_oss"),
    ],
)
def test_legacy_enum_member_names_resolve_to_stable_drive_ids(legacy: str, expected: str) -> None:
    assert provider_id(legacy) == expected
    assert default_drive_registry().require(legacy).id == expected


def test_builtin_drives_implement_the_shared_object_storage_contract(tmp_path) -> None:
    adapters = default_drive_registry().create_all(
        config=SimpleNamespace(),
        local_root=tmp_path,
    )
    operations = (
        "prepare_mount_root",
        "local_path",
        "physical_id",
        "validate_mount",
        "list_objects",
        "iter_objects",
        "head_object",
        "put_object",
        "open_object",
        "copy_object",
        "delete_object",
        "mount_has_objects",
        "purge_mount",
        "presign_put",
        "create_multipart_upload",
        "presign_part",
        "upload_part",
        "list_parts",
        "complete_multipart_upload",
        "abort_multipart_upload",
        "build_download_url",
        "configure_cors",
        "validate_completed_object",
    )
    for driver_id, adapter in adapters.items():
        missing = [operation for operation in operations if not callable(getattr(adapter, operation, None))]
        assert missing == [], f"{driver_id} is missing {missing}"


def test_credential_vault_encrypts_and_rejects_wrong_key() -> None:
    vault = CredentialVault("a" * 48)
    ciphertext = vault.seal({"access_key_id": "id", "access_key_secret": "secret"})
    assert "secret" not in ciphertext
    assert vault.open(ciphertext) == {"access_key_id": "id", "access_key_secret": "secret"}
    with pytest.raises(RuntimeError, match="cannot be decrypted"):
        CredentialVault("b" * 48).open(ciphertext)


def test_policy_read_never_contains_credentials() -> None:
    policy = StoragePolicyRead(
        id=1,
        name="oss",
        provider="aliyun_oss",
        bucket_name="bucket",
        credentials_configured=True,
        credential_fields=["access_key_id", "access_key_secret"],
    )
    dumped = policy.model_dump()
    assert dumped["credentials_configured"] is True
    assert "credentials" not in dumped
    assert "access_key_id" not in dumped
    assert "access_key_secret" not in dumped


def test_v6_sqlite_migration_adds_versioned_encrypted_payload() -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table(
        "storage_policies",
        metadata,
        Column("id", String, primary_key=True),
        Column("provider", String(12), nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        upgrade_v6(connection)
    columns = {item["name"]: item for item in inspect(engine).get_columns("storage_policies")}
    assert columns["credential_ciphertext"]["nullable"] is True
    assert columns["credential_version"]["nullable"] is False


def test_v7_normalizes_legacy_provider_values() -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    policies = Table(
        "storage_policies",
        metadata,
        Column("id", String, primary_key=True),
        Column("provider", String(64), nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(policies.insert(), [
            {"id": "1", "provider": "TENCENT_COS"},
            {"id": "2", "provider": "local_fs"},
            {"id": "3", "provider": "custom_drive"},
        ])
        upgrade_v7(connection)
        values = list(connection.scalars(select(policies.c.provider).order_by(policies.c.id)))
    assert values == ["tencent_cos", "local_fs", "custom_drive"]


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("mysql", "MODIFY COLUMN provider VARCHAR(64) NOT NULL"),
        ("postgresql", "ALTER COLUMN provider TYPE VARCHAR(64)"),
    ],
)
def test_v6_expands_provider_column_for_server_databases(
    monkeypatch: pytest.MonkeyPatch,
    dialect: str,
    expected: str,
) -> None:
    class Inspector:
        def get_table_names(self):
            return ["storage_policies"]

        def get_columns(self, _table):
            return [
                {"name": "provider"},
                {"name": "credential_ciphertext"},
                {"name": "credential_version"},
            ]

    statements: list[str] = []
    connection = SimpleNamespace(
        dialect=SimpleNamespace(name=dialect),
        execute=lambda statement: statements.append(str(statement)),
    )
    import db.mig.v6 as migration

    monkeypatch.setattr(migration, "inspect", lambda _connection: Inspector())
    upgrade_v6(connection)
    assert any(expected in statement for statement in statements)


@pytest.mark.asyncio
async def test_legacy_cos_credentials_are_backed_up_encrypted_and_cleared(tmp_path) -> None:
    config = load_config(tmp_path)
    parser = ConfigParser()
    parser.read(config.config_path, encoding="utf-8")
    parser["tencent_cos"]["secret_id"] = "legacy-test-id"
    parser["tencent_cos"]["secret_key"] = "legacy-test-secret"
    with config.config_path.open("w", encoding="utf-8") as stream:
        parser.write(stream)
    config = load_config(tmp_path)

    engine = make_async_engine(config)
    await create_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        policy = StoragePolicy(
            name="legacy-cos",
            provider="tencent_cos",
            bucket_name="legacy-bucket",
            region="ap-guangzhou",
        )
        session.add(policy)
        await session.commit()

        manager = StorageManager(config=config, local_root=config.local_upload_root_path)
        assert await migrate_storage_credentials(
            session,
            config=config,
            storage_manager=manager,
        ) == 1
        await session.refresh(policy)

        assert policy.secret_id is None
        assert policy.secret_key is None
        assert "legacy-test-secret" not in (policy.credential_ciphertext or "")
        assert manager.credentials_for(policy) == {
            "secret_id": "legacy-test-id",
            "secret_key": "legacy-test-secret",
        }

        wrong_config = load_config(tmp_path)
        wrong_config.storage.credential_key = "wrong-key-for-startup-validation"
        wrong_manager = StorageManager(
            config=wrong_config,
            local_root=wrong_config.local_upload_root_path,
        )
        with pytest.raises(RuntimeError, match="cannot be decrypted"):
            await migrate_storage_credentials(
                session,
                config=wrong_config,
                storage_manager=wrong_manager,
            )

    cleared = ConfigParser()
    cleared.read(config.config_path, encoding="utf-8")
    assert cleared.get("tencent_cos", "secret_id") == ""
    assert cleared.get("tencent_cos", "secret_key") == ""
    assert list(tmp_path.glob("config.ini.before-drive-credentials-*.bak"))
    assert list((tmp_path / "data").glob("cloudrave.db.before-drive-credentials-*.bak"))
    await engine.dispose()


def test_oss_encryption_mapping_covers_oss_and_kms() -> None:
    adapter = AliyunOssAdapter()
    policy = lambda extra: SimpleNamespace(extra=extra)
    assert adapter._encryption_args(policy({"server_side_encryption": "SSE-OSS"})) == {
        "server_side_encryption": "AES256"
    }
    assert adapter._encryption_args(policy({
        "server_side_encryption": "SSE-KMS",
        "kms_key_id": "key-id",
    })) == {
        "server_side_encryption": "KMS",
        "server_side_encryption_key_id": "key-id",
    }


def test_oss_cors_merge_preserves_unrelated_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    class Model(SimpleNamespace):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    unrelated = Model(
        allowed_origins=["https://existing.example"],
        allowed_methods=["GET"],
        allowed_headers=[],
        expose_headers=[],
    )

    class Client:
        written = None

        def get_bucket_cors(self, _request):
            return Model(cors_configuration=Model(cors_rules=[unrelated]))

        def put_bucket_cors(self, request):
            self.written = request.cors_configuration

    fake_oss = SimpleNamespace(
        GetBucketCorsRequest=Model,
        PutBucketCorsRequest=Model,
        CORSRule=Model,
        CORSConfiguration=Model,
    )
    import store.oss.driver as module

    monkeypatch.setattr(module, "oss", fake_oss)
    adapter = AliyunOssAdapter()
    client = Client()
    monkeypatch.setattr(adapter, "_client_for", lambda _policy: client)
    monkeypatch.setattr(adapter, "_bucket", lambda _policy: "bucket")
    result = adapter.configure_cors(
        SimpleNamespace(extra={"cors_origins": ["https://cloudrave.example"]}),
        SimpleNamespace(),
    )
    assert result["rule_count"] == 2
    assert client.written.cors_rules[0] is unrelated
    added = client.written.cors_rules[1]
    assert added.allowed_methods == ["PUT", "GET", "HEAD", "POST", "DELETE"]
    assert "ETag" in added.expose_headers


def test_oss_hard_delete_removes_versions_and_delete_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    class Model(SimpleNamespace):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    class Paginator:
        def iter_page(self, _request):
            yield Model(
                version=[Model(key="prefix/file.txt", version_id="version-1")],
                delete_marker=[Model(key="prefix/file.txt", version_id="marker-1")],
            )

    class Client:
        def __init__(self):
            self.deleted: list[tuple[str, str | None]] = []

        def delete_object(self, request):
            self.deleted.append((request.key, getattr(request, "version_id", None)))

        def list_object_versions_paginator(self):
            return Paginator()

    fake_oss = SimpleNamespace(
        DeleteObjectRequest=Model,
        ListObjectVersionsRequest=Model,
    )
    import store.oss.driver as module

    monkeypatch.setattr(module, "oss", fake_oss)
    adapter = AliyunOssAdapter()
    client = Client()
    monkeypatch.setattr(adapter, "_client_for", lambda _policy: client)
    monkeypatch.setattr(adapter, "_bucket", lambda _policy: "bucket")
    monkeypatch.setattr(adapter, "_key", lambda _policy, _mount, _key: "prefix/file.txt")

    assert adapter.delete_object(SimpleNamespace(), SimpleNamespace(), "file.txt", hard_delete=True)
    assert client.deleted == [
        ("prefix/file.txt", None),
        ("prefix/file.txt", "version-1"),
        ("prefix/file.txt", "marker-1"),
    ]


def test_oss_stream_supports_sized_reads() -> None:
    class Body:
        closed = False

        def iter_bytes(self, **_kwargs):
            yield b"abc"
            yield b"defgh"

        def close(self):
            self.closed = True

    body = Body()
    stream = OssStream(body, chunk_size=3)
    assert stream.read(2) == b"ab"
    assert stream.read(4) == b"cdef"
    assert stream.read() == b"gh"
    stream.close()
    assert body.closed is True
