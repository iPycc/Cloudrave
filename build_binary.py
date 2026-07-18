"""Build a standalone Cloudrave binary with PyInstaller.

Run from the Cloudrave/ directory after the frontend has been copied into
``assets/``:

    python build_binary.py

The single-file executable is written to ``dist/cloudrave`` on Linux/macOS
and ``dist/cloudrave.exe`` on Windows.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ASSETS_DIR = ROOT / "assets"
SPEC_FILE = ROOT / "cloudrave.spec"
DIST_DIR = ROOT / "dist"
EXECUTABLE = DIST_DIR / ("cloudrave.exe" if sys.platform == "win32" else "cloudrave")


def main() -> int:
    if not shutil.which("pyinstaller"):
        print("PyInstaller not found. Install it with: pip install pyinstaller", file=sys.stderr)
        return 1

    if not ASSETS_DIR.is_dir() or not (ASSETS_DIR / "index.html").is_file():
        print(
            "Warning: assets/ is empty. The binary will be built in API-only mode.\n"
            "To build a fullstack binary, run the fullstack build first:",
            "    python ../build-fullstack.py",
            file=sys.stderr,
        )

    if EXECUTABLE.exists():
        print(f"Cleaning previous build at {EXECUTABLE}")
        EXECUTABLE.unlink()

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC_FILE),
        "--clean",
        "--noconfirm",
    ]
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("PyInstaller failed.", file=sys.stderr)
        return result.returncode

    print(f"\nExecutable created at: {EXECUTABLE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
