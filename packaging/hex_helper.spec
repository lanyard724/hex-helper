# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller --onedir spec for HexHelper commercial Windows client."""

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent
SRC_DIR = ROOT / "src"
ASSETS_DIR = SRC_DIR / "hex_helper" / "assets"
SEED_DIR = ROOT / "build" / "bundled_seed"
ICON_FILE = ASSETS_DIR / "hex_helper.ico"

datas = [
    (str(ASSETS_DIR), "hex_helper/assets"),
    (str(ROOT / "packaging" / "apply_update.bat"), "packaging"),
]
if SEED_DIR.is_dir():
    datas.append((str(SEED_DIR), "bundled_seed"))

binaries = []
hiddenimports = [
    "winrt.runtime",
    "winrt.system",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.globalization",
    "winrt.windows.graphics.imaging",
    "winrt.windows.media.ocr",
    "winrt.windows.storage.streams",
    "dxcam",
    "comtypes",
    "comtypes.stream",
    "mss",
    "qrcode",
    "PIL",
    "PIL.Image",
    "cv2",
    "numpy",
    "httpx",
    "httpcore",
    "h11",
    "h2",
    "certifi",
    "psutil",
    "hex_helper.edition",
    "hex_helper.cloud",
    "hex_helper.cloud.session_store",
    "hex_helper.cloud.client",
    "hex_helper.cloud.updater",
    "hex_helper.ui.account_dialogs",
    "hex_helper.ui.hex_seal_overlay",
    "hex_helper.ui.hotkey_settings",
    "hex_helper.setup.preflight",
    "hex_helper.ui.setup_wizard",
    "hex_helper.ui.resizable_overlay",
    "hex_helper.vision.augment_icon_matcher",
    "hex_helper.game.lol_locator",
]

winrt_datas, winrt_bins, winrt_hidden = collect_all("winrt")
datas += winrt_datas
binaries += winrt_bins
hiddenimports += winrt_hidden

excludes = [
    "google",
    "google.genai",
    "google.auth",
    "google.generativeai",
    "google.api_core",
    "google.protobuf",
    "googleapis_common_protos",
    "pytest",
    "_pytest",
    "tkinter",
    "_tkinter",
    "matplotlib",
    "scipy",
    "torch",
    "torchvision",
    "torchaudio",
    "faiss",
    "ctranslate2",
    "faster_whisper",
    "PyQt6",
    "PyQt6_sip",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "gradio",
    "gradio_client",
    "selenium",
    "playwright",
    "pandas",
    "numba",
    "llvmlite",
    "onnxruntime",
    "rembg",
    "skimage",
    "IPython",
    "jupyter",
    "notebook",
    "openai",
    "anthropic",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langchain_anthropic",
    "langgraph",
    "fastapi",
    "uvicorn",
    "flask",
    "quart",
    "werkzeug",
    "sqlalchemy",
    "yt_dlp",
    "you_get",
    "edge_tts",
    "nvidia",
    "aiohttp",
    "lxml",
    "openpyxl",
    "python_pptx",
    "docx",
    "pypdf",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtPdf",
]

a = Analysis(
    [str(SRC_DIR / "hex_helper" / "main.py")],
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

# Defensive commercial strip: ensure zero google.genai / google.* entries ever sneak into PYZ/datas
_forbidden_prefixes = ("google.", "google/", "google\\", "genai")
a.pure = [
    entry for entry in a.pure
    if not (entry[0] == "google" or entry[0].startswith(_forbidden_prefixes))
]
a.datas = [
    entry for entry in a.datas
    if not any(part in entry[0].lower().replace("\\", "/") for part in ("google/genai", "google_genai", "google/auth"))
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HexHelper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_FILE) if ICON_FILE.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="HexHelper",
)
