# PyInstaller spec for the face-recognition kiosk.
#
#   pyinstaller facekiosk.spec
#
# Onefile: single binary, extracted to a per-launch temp dir at runtime for
# the read-only bundled resources (templates/static/models) — safe, because
# facekiosk/config.py redirects writable state (gallery, db, key, logs) to
# sit next to the built executable, not inside that temp dir.
#
# Same spec is intended to work for a Windows build too (PyInstaller spec
# syntax is cross-platform) — but it must actually run on Windows to produce
# a .exe; this file alone doesn't make that happen from macOS.

import os

_datas = [
    ("facekiosk/web", "facekiosk/web"),
    ("models", "models"),
]
if os.path.isdir("vendor"):
    # A static ffmpeg (facekiosk.fetch_ffmpeg) so the built exe needs no
    # separate ffmpeg install. datas, not binaries — it's a standalone static
    # executable, not a shared library PyInstaller should try to relink.
    _datas.append(("vendor", "vendor"))
else:
    print("! vendor/ffmpeg not found — run `python -m facekiosk.fetch_ffmpeg` first "
          "if you want ffmpeg bundled. Building without it; the exe will fall back "
          "to a system-installed ffmpeg on PATH.")

a = Analysis(
    ["run_kiosk.py"],
    pathex=[],
    binaries=[],
    datas=_datas,
    hiddenimports=["cv2"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="facekiosk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
