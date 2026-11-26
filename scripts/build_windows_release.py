"""Reproducible Windows commercial release builder and clean-environment binary verifier."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDITION_FILE = ROOT / "src" / "hex_helper" / "edition.py"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
SEED_STAGING_DIR = BUILD_DIR / "bundled_seed"
PAYLOAD_ZIP = BUILD_DIR / "HexHelper_payload.zip"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _default_source_cache_dir() -> Path:
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        return Path(local_app) / "HexHelper" / "cache"
    return Path.home() / ".hex_helper" / "cache"


def stage_bundled_seed(source_cache_dir: Path, staging_dir: Path) -> tuple[int, int]:
    """Copy datasets/, static/ JSON files, and up to 160 common item PNG icons into build/bundled_seed/."""
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    file_count = 0
    total_bytes = 0
    for subdir_name in ("datasets", "static"):
        src_sub = source_cache_dir / subdir_name
        if not src_sub.is_dir():
            raise FileNotFoundError(
                f"Missing required seed source directory: {src_sub}. "
                "Run HexHelper once or sync datasets before packaging."
            )
        for src in src_sub.rglob("*"):
            if not src.is_file():
                continue
            if src.suffix.lower() != ".json":
                continue
            rel = src.relative_to(source_cache_dir)
            dst = staging_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            file_count += 1
            total_bytes += src.stat().st_size

    # Stage common 4-digit ARAM item PNG icons (<1.1 MB total) for zero-flicker first match
    item_icons_dir = source_cache_dir / "icons" / "item"
    if item_icons_dir.is_dir():
        icon_budget_bytes = 1_150_000
        icon_bytes = 0
        icon_count = 0
        for src in sorted(item_icons_dir.glob("*.png")):
            if not src.is_file():
                continue
            stem = src.stem
            if not (stem.isdigit() and len(stem) <= 4):
                continue
            sz = src.stat().st_size
            if sz <= 200 or (icon_bytes + sz) > icon_budget_bytes or icon_count >= 160:
                continue
            rel = src.relative_to(source_cache_dir)
            dst = staging_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            icon_count += 1
            icon_bytes += sz
            file_count += 1
            total_bytes += sz

    if file_count == 0:
        raise RuntimeError(f"No JSON seed files staged from {source_cache_dir}")
    return file_count, total_bytes


def _patch_edition_text(
    original: str,
    *,
    version: str,
    edition: str,
    cloud_api_base: str | None = None,
) -> str:
    patched, ver_count = re.subn(
        r'^(APP_VERSION\s*=\s*)"[^"]*"',
        rf'\g<1>"{version}"',
        original,
        count=1,
        flags=re.MULTILINE,
    )
    if ver_count != 1:
        raise RuntimeError(f"Expected 1 APP_VERSION assignment in edition.py, patched {ver_count}")

    patched, ed_count = re.subn(
        r'^(_COMPILED_EDITION\s*=\s*)"[^"]*"',
        rf'\g<1>"{edition}"',
        patched,
        count=1,
        flags=re.MULTILINE,
    )
    if ed_count != 1:
        raise RuntimeError(f"Expected 1 _COMPILED_EDITION assignment in edition.py, patched {ed_count}")

    if cloud_api_base:
        patched, api_count = re.subn(
            r'("HEX_CLOUD_API_BASE",\s*\n?\s*)"[^"]*"',
            rf'\g<1>"{cloud_api_base.rstrip("/")}"',
            patched,
            count=1,
            flags=re.MULTILINE,
        )
        if api_count != 1:
            raise RuntimeError(f"Expected 1 HEX_CLOUD_API_BASE default in edition.py, patched {api_count}")

    return patched


def _is_forbidden_devlab_path(rel_norm: str) -> bool:
    low = rel_norm.replace("\\", "/").lower()
    if any(
        token in low
        for token in (
            "hex_helper/devlab",
            "hex_helper.devlab",
            "/devlab/",
            "/devlab.",
            "flight_recorder",
            "hex_helper/bridge/agent_bridge",
            "hex_helper.bridge.agent_bridge",
            "/agent_bridge.",
            "/agent_bridge/",
        )
    ):
        return True
    return low.startswith(("devlab/", "devlab.", "agent_bridge.", "agent_bridge/")) or low in (
        "devlab",
        "agent_bridge",
    )


def _is_forbidden_devlab_module(mod_name: str) -> bool:
    low = str(mod_name).lower()
    return (
        low == "hex_helper.devlab"
        or low.startswith("hex_helper.devlab.")
        or low == "devlab"
        or low.startswith("devlab.")
        or ".devlab." in low
        or low.endswith(".devlab")
        or "flight_recorder" in low
        or low == "hex_helper.bridge.agent_bridge"
        or low.startswith("hex_helper.bridge.agent_bridge.")
    )


def audit_commercial_bundle(bundle_dir: Path) -> dict[str, object]:
    """Fail if any google.genai / google.auth or DevLab / agent_bridge artifacts exist in _internal or PYZ."""
    internal_dir = bundle_dir / "_internal"
    if not internal_dir.is_dir():
        raise FileNotFoundError(f"Expected PyInstaller _internal directory missing: {internal_dir}")

    offending_paths: list[str] = []
    devlab_offending_paths: list[str] = []
    total_size = 0
    file_count = 0
    for p in bundle_dir.rglob("*"):
        rel_norm = str(p.relative_to(bundle_dir)).replace("\\", "/").lower()
        if p.is_dir():
            if _is_forbidden_devlab_path(rel_norm + "/"):
                devlab_offending_paths.append(rel_norm + "/")
            continue
        if not p.is_file():
            continue
        file_count += 1
        total_size += p.stat().st_size
        if any(
            token in rel_norm
            for token in (
                "google/genai",
                "google_genai",
                "google-genai",
                "/genai/",
                "/genai.",
                "google/auth",
                "google_generativeai",
            )
        ):
            offending_paths.append(rel_norm)
        if _is_forbidden_devlab_path(rel_norm):
            devlab_offending_paths.append(rel_norm)

    # Also inspect embedded PYZ archive TOC to verify google.genai and devlab/agent_bridge are absent from frozen bytecode
    pyz_hits: list[str] = []
    devlab_pyz_hits: list[str] = []
    pyz_module_count = 0
    exe_path = bundle_dir / "HexHelper.exe"
    if exe_path.is_file():
        from PyInstaller.archive.readers import CArchiveReader, PKG_ITEM_PYZ

        pkg = CArchiveReader(str(exe_path))
        for name, entry in pkg.toc.items():
            typecode = entry[4] if len(entry) >= 5 else ""
            if typecode == PKG_ITEM_PYZ:
                pyz_reader = pkg.open_embedded_archive(name)
                pyz_module_count += len(pyz_reader.toc)
                for mod_name in pyz_reader.toc:
                    low = str(mod_name).lower()
                    if low == "google" or low.startswith("google.") or "genai" in low:
                        pyz_hits.append(str(mod_name))
                    if _is_forbidden_devlab_module(str(mod_name)):
                        devlab_pyz_hits.append(str(mod_name))

    if pyz_module_count == 0:
        raise RuntimeError(f"Audit failed: could not inspect embedded PYZ archive in {exe_path}")

    google_genai_clean = not (offending_paths or pyz_hits)
    devlab_clean = not (devlab_offending_paths or devlab_pyz_hits)

    if not google_genai_clean or not devlab_clean:
        reasons: list[str] = []
        if not google_genai_clean:
            reasons.append(f"Google/GenAI artifacts found: files={offending_paths}, pyz={pyz_hits}")
        if not devlab_clean:
            reasons.append(
                f"DevLab/AgentBridge artifacts found (devlab_clean=False): "
                f"files={devlab_offending_paths}, pyz={devlab_pyz_hits}"
            )
        raise RuntimeError("Commercial bundle audit FAILED! " + "; ".join(reasons))

    return {
        "bundle_files": file_count,
        "bundle_bytes": total_size,
        "bundle_mb": round(total_size / (1024 * 1024), 2),
        "pyz_modules": pyz_module_count,
        "google_genai_clean": google_genai_clean,
        "devlab_clean": devlab_clean,
    }


def _zip_directory(source_dir: Path, zip_path: Path) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for file_path in sorted(source_dir.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, arcname=file_path.relative_to(source_dir))
    return zip_path


def _build_patch_zip(bundle_dir: Path, patch_zip_path: Path) -> Path:
    """Create a lightweight code-only hotfix zip containing ONLY HexHelper.exe.

    CRITICAL INVARIANT: apply_update.bat runs ``robocopy ... /MIR`` on ``_internal``
    whenever ``_internal`` exists inside the extracted zip. Therefore a patch zip MUST
    contain ONLY ``HexHelper.exe`` and NEVER a partial ``_internal/`` directory.
    """
    exe_path = bundle_dir / "HexHelper.exe"
    if not exe_path.is_file():
        raise FileNotFoundError(f"Cannot build patch zip; missing frozen executable: {exe_path}")

    patch_zip_path.parent.mkdir(parents=True, exist_ok=True)
    if patch_zip_path.exists():
        patch_zip_path.unlink()

    with zipfile.ZipFile(patch_zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.write(exe_path, arcname="HexHelper.exe")

    with zipfile.ZipFile(patch_zip_path, "r") as zf:
        entries = zf.namelist()
        if entries != ["HexHelper.exe"]:
            raise RuntimeError(
                f"Patch zip invariant violated: expected ['HexHelper.exe'], got {entries!r}"
            )
    return patch_zip_path


def verify_clean_env_binary(exe_path: Path, expected_edition: str) -> dict[str, object]:
    """Run frozen HexHelper.exe --preflight-check-only in a stripped clean environment."""
    if not exe_path.is_file():
        raise FileNotFoundError(f"Frozen executable not found: {exe_path}")

    with tempfile.TemporaryDirectory(prefix="hex_clean_verify_") as td:
        tmp_dir = Path(td)
        data_dir = tmp_dir / "appdata"
        local_appdata_dir = tmp_dir / "localappdata"
        local_appdata_dir.mkdir(parents=True, exist_ok=True)
        report_out = tmp_dir / "preflight_report.json"

        clean_env: dict[str, str] = {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
            "PATH": r"C:\Windows\System32;C:\Windows",
            "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
            "TEMP": str(tmp_dir),
            "TMP": str(tmp_dir),
            "USERPROFILE": os.environ.get("USERPROFILE", ""),
            "APPDATA": os.environ.get("APPDATA", ""),
            "LOCALAPPDATA": str(local_appdata_dir),
            "PUBLIC": os.environ.get("PUBLIC", r"C:\Users\Public"),
            "ProgramData": os.environ.get("ProgramData", r"C:\ProgramData"),
            "HEX_HELPER_DATA_DIR": str(data_dir),
            "PYTHONPATH": "",
            "PYTHONHOME": "",
        }
        if expected_edition == "commercial":
            # Adversarial isolation gate: commercial binary must ignore all dev/devlab env overrides
            clean_env.update(
                {
                    "HEX_EDITION": "dev",
                    "HEX_DEVLAB": "1",
                    "HEX_DEVLAB_ROI": "1",
                    "HEX_DEVLAB_DEBUG": "1",
                }
            )

        proc = subprocess.run(
            [
                str(exe_path),
                "--preflight-check-only",
                "--preflight-report-out",
                str(report_out),
            ],
            cwd=str(exe_path.parent),
            env=clean_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Frozen binary exited with code {proc.returncode}: stderr={proc.stderr!r}"
            )
        if not report_out.is_file():
            raise RuntimeError("Frozen binary did not write --preflight-report-out JSON file")

        report = json.loads(report_out.read_text(encoding="utf-8"))
        if report.get("edition") != expected_edition:
            raise AssertionError(
                f"Expected edition={expected_edition!r}, got {report.get('edition')!r}"
            )

        items_by_key = {item["key"]: item for item in report.get("items", [])}
        for req_key in ("vcredist", "winrt_ocr", "seed_data", "lol_path"):
            item = items_by_key.get(req_key)
            if not item or not item.get("ok"):
                raise AssertionError(f"Clean-env check failed for {req_key}: {item}")

        cfg_path = data_dir / "config.json"
        if not cfg_path.is_file():
            raise AssertionError("Frozen binary did not persist config.json")
        saved_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not saved_cfg.get("lockfile_path"):
            raise AssertionError("Persisted config.json missing detected lockfile_path")

        if expected_edition == "commercial":
            devlab_leaks: list[str] = []
            for root_dir in (data_dir, local_appdata_dir):
                if not root_dir.exists():
                    continue
                for p in root_dir.rglob("*"):
                    rel = str(p.relative_to(root_dir)).replace("\\", "/").lower()
                    if "devlab" in rel:
                        devlab_leaks.append(f"{root_dir.name}/{rel}")
            if devlab_leaks:
                raise AssertionError(
                    f"Commercial binary created forbidden devlab artifacts under clean env: {devlab_leaks}"
                )
            for devlab_key in ("devlab_enabled", "devlab_roi_snapshots", "devlab_debug_log"):
                if saved_cfg.get(devlab_key):
                    raise AssertionError(
                        f"Commercial config.json persisted {devlab_key}={saved_cfg.get(devlab_key)!r}"
                    )
            report["devlab_isolated"] = True

        extracted_datasets = list((data_dir / "cache" / "datasets").glob("*/*/heroes.json"))
        if not extracted_datasets:
            raise AssertionError("Bundled seed datasets were not extracted into clean data_dir/cache/datasets")
        extracted_icons = list((data_dir / "cache" / "icons" / "item").glob("*.png"))
        report["extracted_item_icons"] = len(extracted_icons)

        return report


def run_build(args: argparse.Namespace) -> dict[str, object]:
    python_exe = sys.executable
    bundle_dir = DIST_DIR / "HexHelper"
    exe_path = bundle_dir / "HexHelper.exe"

    if getattr(args, "patch_from_existing", False):
        if not exe_path.is_file():
            raise FileNotFoundError(
                f"--patch-from-existing requires an existing built bundle at {exe_path}"
            )
        audit_info = audit_commercial_bundle(bundle_dir) if args.edition == "commercial" else {}
        patch_zip_path = _build_patch_zip(
            bundle_dir,
            DIST_DIR / f"HexHelper_v{args.version}_win64_patch.zip",
        )
        print(
            f"[build] Patch zip created from existing bundle: {patch_zip_path} "
            f"({patch_zip_path.stat().st_size / (1024 * 1024):.2f} MB)"
        )
        return {
            "version": args.version,
            "edition": args.edition,
            "audit": audit_info,
            "artifacts": {
                "patch_zip": {
                    "path": str(patch_zip_path.relative_to(ROOT)),
                    "bytes": patch_zip_path.stat().st_size,
                    "sha256": _sha256_file(patch_zip_path),
                }
            },
        }

    cache_dir = Path(args.seed_cache_dir) if args.seed_cache_dir else _default_source_cache_dir()
    seed_count, seed_bytes = stage_bundled_seed(cache_dir, SEED_STAGING_DIR)
    print(f"[build] Staged {seed_count} seed JSON files ({seed_bytes / (1024 * 1024):.2f} MB) -> {SEED_STAGING_DIR}")

    original_edition_bytes = EDITION_FILE.read_bytes()
    original_edition_text = original_edition_bytes.decode("utf-8")
    portable_zip = DIST_DIR / f"HexHelper_v{args.version}_win64_portable.zip"
    patch_zip_path = None
    setup_exe_path: Path | None = None
    audit_info = {}

    try:
        patched = _patch_edition_text(
            original_edition_text,
            version=args.version,
            edition=args.edition,
            cloud_api_base=args.cloud_api_base or None,
        )
        EDITION_FILE.write_text(patched, encoding="utf-8")

        app_work_dir = BUILD_DIR / "pyinstaller_app"
        app_dist_dir = DIST_DIR / "HexHelper"
        if app_dist_dir.exists():
            shutil.rmtree(app_dist_dir)

        cmd_app = [
            python_exe,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(DIST_DIR),
            "--workpath",
            str(app_work_dir),
            str(ROOT / "packaging" / "hex_helper.spec"),
        ]
        print(f"[build] Running PyInstaller for HexHelper ({args.edition} v{args.version})...")
        subprocess.run(cmd_app, cwd=str(ROOT), check=True)

        audit_info = audit_commercial_bundle(bundle_dir) if args.edition == "commercial" else {}
        print(f"[build] Bundle audit: {audit_info}")

        _zip_directory(bundle_dir, portable_zip)
        print(f"[build] Portable zip created: {portable_zip} ({portable_zip.stat().st_size / (1024 * 1024):.2f} MB)")

        if getattr(args, "build_patch_zip", False):
            patch_zip_path = _build_patch_zip(
                bundle_dir,
                DIST_DIR / f"HexHelper_v{args.version}_win64_patch.zip",
            )
            print(
                f"[build] Code-only patch zip created: {patch_zip_path} "
                f"({patch_zip_path.stat().st_size / (1024 * 1024):.2f} MB)"
            )

        if args.build_setup_exe:
            _zip_directory(bundle_dir, PAYLOAD_ZIP)
            env = os.environ.copy()
            env["HEX_BUILD_VERSION"] = args.version
            setup_work_dir = BUILD_DIR / "pyinstaller_setup"
            cmd_setup = [
                python_exe,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--distpath",
                str(DIST_DIR),
                "--workpath",
                str(setup_work_dir),
                str(ROOT / "packaging" / "hex_helper_installer.spec"),
            ]
            print("[build] Running PyInstaller for double-click setup installer...")
            subprocess.run(cmd_setup, cwd=str(ROOT), env=env, check=True)
            setup_exe_path = DIST_DIR / f"HexHelper_Setup_v{args.version}_win64.exe"
            if not setup_exe_path.is_file():
                raise FileNotFoundError(f"Setup executable missing: {setup_exe_path}")
            print(f"[build] Setup EXE created: {setup_exe_path} ({setup_exe_path.stat().st_size / (1024 * 1024):.2f} MB)")
    finally:
        EDITION_FILE.write_bytes(original_edition_bytes)
        print("[build] Restored original src/hex_helper/edition.py")

    verify_report: dict[str, object] | None = None
    if args.verify_binary:
        print("[verify] Running clean-environment isolated binary verification...")
        verify_report = verify_clean_env_binary(exe_path, expected_edition=args.edition)
        print("[verify] Clean-env verification PASSED:")
        print(json.dumps(verify_report, ensure_ascii=False, indent=2))

    artifacts: dict[str, dict[str, object]] = {
        "portable_zip": {
            "path": str(portable_zip.relative_to(ROOT)),
            "bytes": portable_zip.stat().st_size,
            "sha256": _sha256_file(portable_zip),
        },
        "app_exe": {
            "path": str(exe_path.relative_to(ROOT)),
            "bytes": exe_path.stat().st_size,
            "sha256": _sha256_file(exe_path),
        },
    }
    if patch_zip_path is not None:
        artifacts["patch_zip"] = {
            "path": str(patch_zip_path.relative_to(ROOT)),
            "bytes": patch_zip_path.stat().st_size,
            "sha256": _sha256_file(patch_zip_path),
        }
    if setup_exe_path is not None:
        artifacts["setup_exe"] = {
            "path": str(setup_exe_path.relative_to(ROOT)),
            "bytes": setup_exe_path.stat().st_size,
            "sha256": _sha256_file(setup_exe_path),
        }

    manifest = {
        "version": args.version,
        "edition": args.edition,
        "seed_files": seed_count,
        "seed_bytes": seed_bytes,
        "audit": audit_info,
        "artifacts": artifacts,
        "clean_env_verification": verify_report,
    }
    manifest_path = DIST_DIR / "release_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[build] Release manifest written: {manifest_path}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build HexHelper Windows commercial release")
    parser.add_argument("--version", default="1.0.0", help="Release version string")
    parser.add_argument("--edition", default="commercial", choices=("commercial", "dev"), help="Compiled edition")
    parser.add_argument("--cloud-api-base", default="", help="Optional override for DEFAULT_CLOUD_API_BASE")
    parser.add_argument("--seed-cache-dir", default="", help="Override source cache directory for bundled_seed")
    parser.add_argument("--build-patch-zip", action="store_true", help="Also build lightweight HexHelper.exe-only hotfix patch zip (~8 MB)")
    parser.add_argument("--patch-from-existing", action="store_true", help="Skip PyInstaller and package existing dist/HexHelper/HexHelper.exe into _patch.zip")
    parser.add_argument("--build-setup-exe", action="store_true", help="Build HexHelper_Setup_v<ver>_win64.exe")
    parser.add_argument("--verify-binary", action="store_true", help="Verify frozen HexHelper.exe in clean PATH-stripped env")
    args = parser.parse_args(argv)

    run_build(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
