"""Configuration loading and bootstrap defaults for Cloudrave."""

from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
from os import getenv
from pathlib import Path
import secrets
from urllib.parse import quote_plus


CONFIG_FILENAME = "config.ini"


@dataclass(slots=True)
class AppConfig:
    name: str = "Cloudrave"
    env: str = "development"
    host: str = "0.0.0.0"
    port: int = 1309
    cors_origins: tuple[str, ...] = ("http://localhost:9130", "http://127.0.0.1:9130")
    local_upload_root: str = "upload"
    request_id_header: str = "X-Request-ID"
    default_timezone: str = "Asia/Shanghai"


@dataclass(slots=True)
class DatabaseConfig:
    backend: str = "sqlite"
    url: str = "sqlite+aiosqlite:///cloudrave.db"
    echo: bool = False
    pool_size: int = 10
    max_overflow: int = 20
    pool_pre_ping: bool = True
    pool_recycle: int = 3600
    postgresql_host: str = "127.0.0.1"
    postgresql_port: int = 5432
    postgresql_user: str = "postgres"
    postgresql_password: str = "postgres"
    postgresql_name: str = "cloudrave"
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "root"
    mysql_name: str = "cloudrave"
    sqlite_path: str = "data/cloudrave.db"


@dataclass(slots=True)
class AuthConfig:
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    jwt_secret: str = "change-me-cloudrave-jwt-secret-at-least-32-bytes"
    jwt_issuer: str = "cloudrave"
    password_salt: str = ""
    totp_secret_key: str = ""
    backup_codes_key: str = ""
    share_signing_key: str = ""


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    use_color: bool = True


@dataclass(slots=True)
class TencentCosConfig:
    default_region: str = "ap-guangzhou"
    presign_ttl_seconds: int = 900


@dataclass(slots=True)
class CloudraveConfig:
    app: AppConfig
    database: DatabaseConfig
    auth: AuthConfig
    logging: LoggingConfig
    tencent_cos: TencentCosConfig
    root_dir: Path
    config_path: Path

    @property
    def local_upload_root_path(self) -> Path:
        return (self.root_dir / self.app.local_upload_root).resolve()


def default_config_parser() -> ConfigParser:
    parser = ConfigParser()
    parser["app"] = {
        "name": "Cloudrave",
        "env": "development",
        "host": "0.0.0.0",
        "port": "1309",
        "cors_origins": "http://localhost:9130,http://127.0.0.1:9130",
        "local_upload_root": "upload",
        "request_id_header": "X-Request-ID",
        "default_timezone": "Asia/Shanghai",
    }
    parser["database"] = {
        "database": "sqlite",
        "url": "",
        "echo": "false",
        "pool_size": "10",
        "max_overflow": "20",
        "pool_pre_ping": "true",
        "pool_recycle": "3600",
        "postgresql_host": "127.0.0.1",
        "postgresql_port": "5432",
        "postgresql_user": "postgres",
        "postgresql_password": "postgres",
        "postgresql_name": "cloudrave",
        "mysql_host": "127.0.0.1",
        "mysql_port": "3306",
        "mysql_user": "root",
        "mysql_password": "root",
        "mysql_name": "cloudrave",
        "sqlite_path": "data/cloudrave.db",
    }
    parser["auth"] = {
        "access_token_ttl_minutes": "15",
        "refresh_token_ttl_days": "30",
        "jwt_secret": "",
        "jwt_issuer": "cloudrave",
        "password_salt": "",
        "totp_secret_key": "",
        "backup_codes_key": "",
    }
    _ensure_auth_secrets(parser)
    parser["logging"] = {
        "share_signing_key": "",
        "level": "INFO",
        "use_color": "true",
    }
    parser["tencent_cos"] = {
        "default_region": "ap-guangzhou",
        "presign_ttl_seconds": "900",
    }
    return parser


def ensure_config_file(root_dir: Path) -> Path:
    config_path = root_dir / CONFIG_FILENAME
    if config_path.exists():
        return config_path

    parser = default_config_parser()
    config_path.write_text(_serialize_config(parser), encoding="utf-8")
    return config_path


def load_config(root_dir: Path) -> CloudraveConfig:
    config_path = ensure_config_file(root_dir)
    parser = ConfigParser()
    parser.read(config_path, encoding="utf-8")
    if _ensure_auth_secrets(parser):
        config_path.write_text(_serialize_config(parser), encoding="utf-8")

    app = AppConfig(
        name=parser.get("app", "name", fallback="Cloudrave"),
        env=parser.get("app", "env", fallback="development"),
        host=parser.get("app", "host", fallback="0.0.0.0"),
        port=parser.getint("app", "port", fallback=1309),
        cors_origins=_split_csv(
            parser.get("app", "cors_origins", fallback="http://localhost:9130,http://127.0.0.1:9130")
        ),
        local_upload_root=parser.get("app", "local_upload_root", fallback="upload"),
        request_id_header=parser.get("app", "request_id_header", fallback="X-Request-ID"),
        default_timezone=parser.get("app", "default_timezone", fallback="Asia/Shanghai"),
    )
    database_backend = parser.get("database", "database", fallback="sqlite").strip().lower() or "sqlite"
    database_url = _resolve_database_url(parser, root_dir, database_backend)
    database = DatabaseConfig(
        backend=_detect_database_backend(database_url, database_backend),
        url=database_url,
        echo=parser.getboolean("database", "echo", fallback=False),
        pool_size=parser.getint("database", "pool_size", fallback=10),
        max_overflow=parser.getint("database", "max_overflow", fallback=20),
        pool_pre_ping=parser.getboolean("database", "pool_pre_ping", fallback=True),
        pool_recycle=parser.getint("database", "pool_recycle", fallback=3600),
        postgresql_host=parser.get("database", "postgresql_host", fallback="127.0.0.1"),
        postgresql_port=parser.getint("database", "postgresql_port", fallback=5432),
        postgresql_user=parser.get("database", "postgresql_user", fallback="postgres"),
        postgresql_password=parser.get("database", "postgresql_password", fallback="postgres"),
        postgresql_name=parser.get("database", "postgresql_name", fallback="cloudrave"),
        mysql_host=parser.get("database", "mysql_host", fallback="127.0.0.1"),
        mysql_port=parser.getint("database", "mysql_port", fallback=3306),
        mysql_user=parser.get("database", "mysql_user", fallback="root"),
        mysql_password=parser.get("database", "mysql_password", fallback="root"),
        mysql_name=parser.get("database", "mysql_name", fallback="cloudrave"),
        sqlite_path=parser.get("database", "sqlite_path", fallback="data/cloudrave.db"),
    )
    auth = AuthConfig(
        access_token_ttl_minutes=parser.getint("auth", "access_token_ttl_minutes", fallback=15),
        refresh_token_ttl_days=parser.getint("auth", "refresh_token_ttl_days", fallback=30),
        jwt_secret=parser.get("auth", "jwt_secret", fallback="change-me-cloudrave-jwt-secret-at-least-32-bytes"),
        jwt_issuer=parser.get("auth", "jwt_issuer", fallback="cloudrave"),
        password_salt=parser.get("auth", "password_salt", fallback=""),
        totp_secret_key=parser.get("auth", "totp_secret_key", fallback=""),
        backup_codes_key=parser.get("auth", "backup_codes_key", fallback=""),
    )
    logging = LoggingConfig(
        level=parser.get("logging", "level", fallback="INFO"),
        use_color=parser.getboolean("logging", "use_color", fallback=True),
    )
    tencent_cos = TencentCosConfig(
        share_signing_key=parser.get("auth", "share_signing_key", fallback=""),
        default_region=parser.get("tencent_cos", "default_region", fallback="ap-guangzhou"),
        presign_ttl_seconds=parser.getint("tencent_cos", "presign_ttl_seconds", fallback=900),
    )
    return CloudraveConfig(
        app=app,
        database=database,
        auth=auth,
        logging=logging,
        tencent_cos=tencent_cos,
        root_dir=root_dir,
        config_path=config_path,
    )


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _resolve_database_url(parser: ConfigParser, root_dir: Path, backend: str) -> str:
    env_url = getenv("CLOUDRAVE_DATABASE_URL")
    if env_url:
        return env_url

    explicit_url = parser.get("database", "url", fallback="").strip()
    if explicit_url:
        return explicit_url

    if backend == "postgresql":
        user = quote_plus(parser.get("database", "postgresql_user", fallback="postgres"))
        password = quote_plus(parser.get("database", "postgresql_password", fallback="postgres"))
        host = parser.get("database", "postgresql_host", fallback="127.0.0.1")
        port = parser.getint("database", "postgresql_port", fallback=5432)
        name = parser.get("database", "postgresql_name", fallback="cloudrave")
        return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"

    if backend == "mysql":
        user = quote_plus(parser.get("database", "mysql_user", fallback="root"))
        password = quote_plus(parser.get("database", "mysql_password", fallback="root"))
        host = parser.get("database", "mysql_host", fallback="127.0.0.1")
        port = parser.getint("database", "mysql_port", fallback=3306)
        name = parser.get("database", "mysql_name", fallback="cloudrave")
        return f"mysql+aiomysql://{user}:{password}@{host}:{port}/{name}"

    sqlite_path = parser.get("database", "sqlite_path", fallback="data/cloudrave.db")
    resolved_path = (root_dir / sqlite_path).resolve() if not Path(sqlite_path).is_absolute() else Path(sqlite_path)
    return f"sqlite+aiosqlite:///{resolved_path.as_posix()}"


def _detect_database_backend(url: str, fallback: str) -> str:
    normalized = url.lower()
    if normalized.startswith("postgresql"):
        return "postgresql"
    if normalized.startswith("mysql"):
        return "mysql"
    if normalized.startswith("sqlite"):
        return "sqlite"
    return fallback


def _ensure_auth_secrets(parser: ConfigParser) -> bool:
    changed = False
    if not parser.has_section("auth"):
        parser["auth"] = {}
        changed = True

    jwt_secret = parser.get("auth", "jwt_secret", fallback="").strip()
    if not jwt_secret or jwt_secret.startswith("change-me-"):
        parser["auth"]["jwt_secret"] = _generate_secret(48)
        changed = True

    password_salt = parser.get("auth", "password_salt", fallback="").strip()
    if not password_salt:
        parser["auth"]["password_salt"] = _generate_secret(32)
        changed = True

    totp_secret_key = parser.get("auth", "totp_secret_key", fallback="").strip()
    if not totp_secret_key:
        parser["auth"]["totp_secret_key"] = _generate_secret(32)
        changed = True

    backup_codes_key = parser.get("auth", "backup_codes_key", fallback="").strip()
    if not backup_codes_key:
        parser["auth"]["backup_codes_key"] = _generate_secret(32)
        changed = True

    return changed


def _generate_secret(length: int) -> str:
    return secrets.token_urlsafe(length)


def _serialize_config(parser: ConfigParser) -> str:
    lines: list[str] = []
    share_signing_key = parser.get("auth", "share_signing_key", fallback="").strip()
    if not share_signing_key:
        parser["auth"]["share_signing_key"] = _generate_secret(48)
        changed = True

    for section in parser.sections():
        lines.append(f"[{section}]")
        for key, value in parser[section].items():
            lines.append(f"{key} = {value}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
