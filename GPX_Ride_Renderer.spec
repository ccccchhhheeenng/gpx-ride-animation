# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller onedir build for GPX Ride Renderer.

This spec uses the Windows windowed/noconsole bootloader. GPX_Ride_Renderer.py restores
stdout/stderr from inherited pipe handles in render/benchmark subprocesses, so the Tkinter
Log window keeps receiving output without showing a terminal for the GUI.
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

project_dir = Path.cwd()
entry_script = project_dir / "GPX_Ride_Renderer.py"

if not entry_script.is_file():
    raise SystemExit(f"Cannot find entry script: {entry_script}")

# Contextily / xyzservices / rasterio contain provider data and native pieces that
# are safer to collect explicitly for a portable build.
datas = []
binaries = []
hiddenimports = []

for package in (
    "contextily",
    "xyzservices",
    "rasterio",
    "imageio_ffmpeg",
):
    try:
        d, b, h = collect_all(package)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:
        print(f"[SPEC] collect_all({package}) skipped: {exc}")

for package in ("gpxpy", "contextily"):
    try:
        hiddenimports += collect_submodules(package)
    except Exception as exc:
        print(f"[SPEC] collect_submodules({package}) skipped: {exc}")

for package in (
    "contextily",
    "xyzservices",
    "rasterio",
    "requests",
    "certifi",
    "imageio-ffmpeg",
):
    try:
        datas += copy_metadata(package)
    except Exception:
        pass

# Common imports that can otherwise be missed in some combinations of hooks.
hiddenimports += [
    "cv2",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "PIL.ImageFilter",
    "matplotlib.colors",
    "tkinter",
    "tkinter.ttk",
]

# Deduplicate hidden imports while preserving order.
hiddenimports = list(dict.fromkeys(hiddenimports))

a = Analysis(
    [str(entry_script)],
    pathex=[str(project_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GPX Ride Renderer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="GPX Ride Renderer",
)
