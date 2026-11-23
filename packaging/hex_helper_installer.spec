# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller --onefile spec for HexHelper_Setup_v<ver>_win64.exe double-click installer."""

from __future__ import annotations

import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent
SRC_DIR = ROOT / "src"
EDITION_TEXT = (SRC_DIR / "hex_helper" / "edition.py").read_text(encoding="utf-8")
_VER_MATCH = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', EDITION_TEXT, re.MULTILINE)
APP_VERSION = os.environ.get("HEX_BUILD_VERSION", _VER_MATCH.group(1) if _VER_MATCH else "1.0.0")

PAYLOAD_ZIP = ROOT / "build" / "HexHelper_payload.zip"
ICON_FILE = SRC_DIR / "hex_helper" / "assets" / "hex_helper.ico"

datas = []
if PAYLOAD_ZIP.is_file():
    datas.append((str(PAYLOAD_ZIP), "."))
if ICON_FILE.is_file():
    datas.append((str(ICON_FILE), "."))

binaries = []
hiddenimports = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "psutil",
    "winrt.runtime",
    "winrt.system",
    "winrt.windows.foundation",
    "winrt.windows.globalization",
    "winrt.windows.media.ocr",
    "hex_helper.edition",
    "hex_helper.config",
    "hex_helper.game.lol_locator",
    "hex_helper.setup.preflight",
]

winrt_datas, winrt_bins, winrt_hidden = collect_all("winrt")
datas += winrt_datas
binaries += winrt_bins
hiddenimports += winrt_hidden

excludes = [
    "PySide6",
    "PyQt6",
    "shiboken6",
    "cv2",
    "numpy",
    "PIL",
    "httpx",
    "httpcore",
    "dxcam",
    "comtypes",
    "mss",
    "qrcode",
    "google",
    "google.genai",
    "google.auth",
    "google.generativeai",
    "pytest",
    "_pytest",
    "matplotlib",
    "scipy",
    "torch",
    "torchvision",
    "torchaudio",
    "faiss",
    "ctranslate2",
    "faster_whisper",
    "gradio",
    "selenium",
    "playwright",
    "pandas",
    "numba",
    "llvmlite",
    "onnxruntime",
    "rembg",
    "skimage",
    "openai",
    "anthropic",
    "langchain",
    "langgraph",
    "fastapi",
    "uvicorn",
    "flask",
    "quart",
    "sqlalchemy",
    "nvidia",
    "aiohttp",
    "lxml",
    "openpyxl",
]

a = Analysis(
    [str(ROOT / "packaging" / "installer_gui.py")],
    pathex=[str(SRC_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f"HexHelper_Setup_v{APP_VERSION}_win64",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_FILE) if ICON_FILE.is_file() else None,
)
