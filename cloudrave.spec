# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for building a single-file Cloudrave binary.

Usage (from the Cloudrave/ directory):
    pyinstaller cloudrave.spec --clean --noconfirm

The output is one self-contained executable. On startup it unpacks to a
temporary directory; runtime files (config.ini, database, uploads) are still
created next to the executable so they persist across runs.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve()
ASSETS_DIR = ROOT / "assets"

# Bundle the assets directory only when it contains a built frontend.
assets_datas = []
if ASSETS_DIR.is_dir() and (ASSETS_DIR / "index.html").is_file():
    assets_datas.append((str(ASSETS_DIR), "assets"))

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=assets_datas,
    hiddenimports=[
        # ASGI server
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.protocols.websockets.wsproto_impl",
        # Database drivers
        "aiosqlite",
        "asyncpg",
        "aiomysql",
        "pymysql",
        "greenlet",
        # Crypto / auth
        "cryptography",
        "cryptography.hazmat.backends.openssl",
        "pyotp",
        "jwt",
        "jwt.algorithms",
        # WebAuthn
        "webauthn",
        "webauthn.helpers",
        "webauthn.helpers.structs",
        "webauthn.helpers.exceptions",
        "webauthn.helpers.cose",
        # Tencent COS
        *collect_submodules("qcloud_cos"),
        # Aliyun OSS and built-in drive registry
        # The async SDK facade has an optional aiohttp dependency. Cloudrave's
        # adapter deliberately uses the synchronous client behind its shared
        # async call bridge, so only collect the modules used by that client.
        *collect_submodules(
            "alibabacloud_oss_v2",
            filter=lambda name: not name.startswith("alibabacloud_oss_v2.aio"),
        ),
        *collect_submodules("drives"),
        *collect_submodules("store.oss"),
        # SQLAlchemy / models
        "sqlalchemy.ext.asyncio",
        "models",
        "models.tables",
        "models.enums",
        "models.blob",
        "models.gc",
        "models.shared",
        # API surface
        "api.v1",
        # Repositories (imported through Dep)
        "repositories.audit_logs",
        "repositories.blobs",
        "repositories.gc",
        "repositories.mounts",
        "repositories.nodes",
        "repositories.objects",
        "repositories.passkeys",
        "repositories.policies",
        "repositories.preview_annotations",
        "repositories.sessions",
        "repositories.shared",
        "repositories.shares",
        "repositories.site_settings",
        "repositories.uploads",
        "repositories.users",
        # Preview codecs
        *collect_submodules("pillow_heif"),
        *collect_submodules("py7zr"),
        # Services referenced dynamically
        "services.admin.ids",
        "services.admin.mount",
        "services.admin.policy",
        "services.admin.scan",
        "services.admin.sync",
        "services.admin.user",
        "services.explorer.archive",
        "services.explorer.download",
        "services.explorer.extract",
        "services.explorer.file",
        "services.explorer.gc",
        "services.explorer.gc_job",
        "services.explorer.pool",
        "services.explorer.preview",
        *collect_submodules("services.explorer.view"),
        "services.explorer.proxy",
        "services.explorer.recycle",
        "services.explorer.share",
        "services.explorer.shared",
        "services.explorer.shared_io",
        "services.explorer.source",
        "services.explorer.up_abort",
        "services.explorer.up_check",
        "services.explorer.up_path",
        "services.explorer.up_plan",
        "services.explorer.upload",
        "services.session.issue",
        "services.session.refresh",
        "services.session.revoke",
        "services.session.two_factor",
        "services.site.settings",
        "services.user.login",
        "services.user.passkey",
        "services.user.profile",
        "services.user.register",
        "services.user.response",
        "services.user.storage",
        "services.user.totp",
        # Storage adapters
        "store.local.fs",
        "store.cos.driver",
        "store.cos.cors",
        "store.cos.key",
        "store.cos.obj",
        "store.cos.parse",
        "store.cos.sec",
        "store.cos.stream",
        "store.cos.upload",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="cloudrave",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
