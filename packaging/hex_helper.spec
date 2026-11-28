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
    "hex_helper.data.locale_pack",
    "hex_helper.data.locale_catalog_data",
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
    "hex_helper.devlab",
    "hex_helper.devlab.recorder",
    "hex_helper.devlab.analyzer",
    "hex_helper.devlab.schema",
    "hex_helper.devlab.ui_observer",
    "hex_helper.devlab.__main__",
    "hex_helper.bridge.agent_bridge",
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

# Defensive commercial strip: ensure zero google.genai / google.* / hex_helper.devlab / agent_bridge entries sneak into PYZ/datas
_forbidden_exact = {
    "google",
    "devlab",
    "hex_helper.devlab",
    "hex_helper.bridge.agent_bridge",
}
_forbidden_prefixes = (
    "google.",
    "google/",
    "google\\",
    "genai",
    "hex_helper.devlab.",
    "hex_helper/devlab",
    "hex_helper\\devlab",
    "devlab.",
    "devlab/",
    "devlab\\",
    "hex_helper.bridge.agent_bridge.",
)
_forbidden_data_tokens = (
    "google/genai",
    "google_genai",
    "google/auth",
    "hex_helper/devlab",
    "hex_helper.devlab",
    "/devlab/",
    "/devlab.",
    "flight_recorder",
    "hex_helper/bridge/agent_bridge",
    "/agent_bridge.",
    "/agent_bridge/",
)


def _is_forbidden_commercial_module(mod_name: str) -> bool:
    low = str(mod_name).lower()
    if low in _forbidden_exact or low.startswith(_forbidden_prefixes):
        return True
    if ".devlab." in low or low.endswith(".devlab") or "flight_recorder" in low:
        return True
    return False


def _is_forbidden_commercial_data(data_name: str) -> bool:
    norm = str(data_name).lower().replace("\\", "/")
    if any(part in norm for part in _forbidden_data_tokens):
        return True
    return norm.startswith(("devlab/", "devlab.", "agent_bridge.", "agent_bridge/")) or norm in (
        "devlab",
        "agent_bridge",
    )


a.pure = [
    entry for entry in a.pure
    if not _is_forbidden_commercial_module(entry[0])
]
a.datas = [
    entry for entry in a.datas
    if not _is_forbidden_commercial_data(entry[0])
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
