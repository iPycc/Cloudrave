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
        # SQLAlchemy / models
        "sqlalchemy.ext.asyncio",
        "app.models",
        "app.models.tables",
        "app.models.enums",
        "app.models.blob",
        "app.models.gc",
        "app.models.shared",
        # API surface
        "app.api.v1",
        # Repositories (imported through Dep)
        "app.repositories.audit_logs",
        "app.repositories.blobs",
        "app.repositories.gc",
        "app.repositories.mounts",
        "app.repositories.nodes",
        "app.repositories.objects",
        "app.repositories.passkeys",
        "app.repositories.policies",
        "app.repositories.sessions",
        "app.repositories.shared",
        "app.repositories.shares",
        "app.repositories.site_settings",
        "app.repositories.uploads",
        "app.repositories.users",
        # Services referenced dynamically
        "app.services.admin.ids",
        "app.services.admin.mount",
        "app.services.admin.policy",
        "app.services.admin.scan",
        "app.services.admin.sync",
        "app.services.admin.user",
        "app.services.explorer.archive",
        "app.services.explorer.download",
        "app.services.explorer.extract",
        "app.services.explorer.file",
        "app.services.explorer.gc",
        "app.services.explorer.gc_job",
        "app.services.explorer.pool",
        "app.services.explorer.preview",
        "app.services.explorer.proxy",
        "app.services.explorer.recycle",
        "app.services.explorer.share",
        "app.services.explorer.shared",
        "app.services.explorer.shared_io",
        "app.services.explorer.source",
        "app.services.explorer.up_abort",
        "app.services.explorer.up_check",
        "app.services.explorer.up_path",
        "app.services.explorer.up_plan",
        "app.services.explorer.upload",
        "app.services.session.issue",
        "app.services.session.refresh",
        "app.services.session.revoke",
        "app.services.session.two_factor",
        "app.services.site.settings",
        "app.services.user.login",
        "app.services.user.passkey",
        "app.services.user.profile",
        "app.services.user.register",
        "app.services.user.response",
        "app.services.user.storage",
        "app.services.user.totp",
        # Storage adapters
        "app.storage_adapters.local_fs",
        "app.storage_adapters.tencent_cos",
        "app.storage_adapters.cos_cors",
        "app.storage_adapters.cos_key",
        "app.storage_adapters.cos_obj",
        "app.storage_adapters.cos_parse",
        "app.storage_adapters.cos_sec",
        "app.storage_adapters.cos_stream",
        "app.storage_adapters.cos_upload",
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
