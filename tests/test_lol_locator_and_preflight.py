from __future__ import annotations

from pathlib import Path

import psutil
import pytest


def _make_mock_lol_install(root: Path, *, with_lockfile: bool = False, exe_name: str = "LeagueClient.exe") -> Path:
    lc_dir = root / "LeagueClient"
    lc_dir.mkdir(parents=True, exist_ok=True)
    (lc_dir / exe_name).write_bytes(b"MZ")
    (root / "TCLS").mkdir(parents=True, exist_ok=True)
    (root / "TCLS" / "Client.exe").write_bytes(b"MZ")
    if with_lockfile:
        (lc_dir / "lockfile").write_text("LeagueClient:1234:54321:secret:https", encoding="utf-8")
    return root


def test_edition_constants_and_dynamic_env_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    import re
    from hex_helper import edition

    assert re.fullmatch(r"\d+\.\d+\.\d+", edition.APP_VERSION)
    assert edition.APP_NAME == "HexHelper"
    assert edition._COMPILED_EDITION == "commercial"
    assert isinstance(edition.EDITION, str)
    assert isinstance(edition.IS_COMMERCIAL, bool)
    assert edition.DEFAULT_CLOUD_API_BASE.startswith("https://")

    monkeypatch.delenv("HEX_EDITION", raising=False)
    assert edition.is_commercial_edition() is True

    monkeypatch.setenv("HEX_EDITION", "commercial")
    assert edition.is_commercial_edition() is True

    monkeypatch.setenv("HEX_EDITION", "  COMMERCIAL ")
    assert edition.is_commercial_edition() is True

    monkeypatch.setenv("HEX_EDITION", "dev")
    assert edition.is_commercial_edition() is True


def test_validate_lol_install_dir_cn_wegame_suffix_and_variants(tmp_path: Path) -> None:
    from hex_helper.game.lol_locator import validate_lol_install_dir

    # CN WeGame folder with (26) suffix
    cn_root = _make_mock_lol_install(tmp_path / "英雄联盟(26)", with_lockfile=True)
    info = validate_lol_install_dir(cn_root, source="test")
    assert info is not None
    assert info.install_dir == cn_root
    assert info.league_client_dir == cn_root / "LeagueClient"
    assert info.lockfile_path == cn_root / "LeagueClient" / "lockfile"
    assert info.source == "test"

    # Passing LeagueClient subdir directly resolves install_dir back to parent
    info_from_lc = validate_lol_install_dir(cn_root / "LeagueClient", source="lc")
    assert info_from_lc is not None
    assert info_from_lc.install_dir == cn_root
    assert info_from_lc.league_client_dir == cn_root / "LeagueClient"

    # Riot flat layout: direct LeagueClient.exe in install root
    riot_root = tmp_path / "Riot Games" / "League of Legends"
    riot_root.mkdir(parents=True)
    (riot_root / "LeagueClient.exe").write_bytes(b"MZ")
    riot_info = validate_lol_install_dir(riot_root, source="riot")
    assert riot_info is not None
    assert riot_info.install_dir == riot_root
    assert riot_info.league_client_dir == riot_root
    assert riot_info.lockfile_path == riot_root / "lockfile"

    # Game/League of Legends.exe layout
    game_only = tmp_path / "GameOnly"
    (game_only / "Game").mkdir(parents=True)
    (game_only / "Game" / "League of Legends.exe").write_bytes(b"MZ")
    game_info = validate_lol_install_dir(game_only, source="game")
    assert game_info is not None
    assert game_info.install_dir == game_only

    # Empty folder fails validation
    empty = tmp_path / "EmptyDir"
    empty.mkdir()
    assert validate_lol_install_dir(empty) is None


def test_extract_root_from_exe_or_path() -> None:
    from hex_helper.game.lol_locator import _extract_root_from_exe_or_path

    expected = Path(r"E:\LOL\英雄联盟(26)")
    assert _extract_root_from_exe_or_path(r"E:\LOL\英雄联盟(26)\TCLS\Client.exe") == expected
    assert _extract_root_from_exe_or_path(r"E:\LOL\英雄联盟(26)\WeGameLauncher\launcher.exe") == expected
    assert _extract_root_from_exe_or_path(r"E:\LOL\英雄联盟(26)\LeagueClient\LeagueClient.exe") == expected
    assert _extract_root_from_exe_or_path(r"E:\LOL\英雄联盟(26)\LeagueClient\lockfile") == expected
    assert _extract_root_from_exe_or_path(r'"E:\LOL\英雄联盟(26)\TCLS\Client.exe",0') == expected
    assert _extract_root_from_exe_or_path(r'"E:\LOL\英雄联盟(26)\英雄联盟卸载.exe" /S') == expected
    assert _extract_root_from_exe_or_path(r"E:\LOL\英雄联盟(26)") == expected
    assert _extract_root_from_exe_or_path("   ") is None
    assert _extract_root_from_exe_or_path("") is None


def test_detect_lol_installations_preferred_dedup_and_running_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hex_helper.game import lol_locator

    pref_root = _make_mock_lol_install(tmp_path / "PreferredLoL" / "英雄联盟(26)")
    running_root = _make_mock_lol_install(tmp_path / "RunningLoL" / "英雄联盟")

    class FakeProc:
        def __init__(self, pid: int, name: str, exe_path: str) -> None:
            self.pid = pid
            self.info = {"pid": pid, "name": name}
            self._exe_path = exe_path

        def exe(self) -> str:
            return self._exe_path

        def cmdline(self) -> list[str]:
            return [self._exe_path]

    monkeypatch.setattr(
        lol_locator.psutil,
        "process_iter",
        lambda attrs=None: [
            FakeProc(1001, "LeagueClientUx.exe", str(running_root / "LeagueClient" / "LeagueClientUx.exe")),
            # Duplicate of running_root via another process should deduplicate
            FakeProc(1002, "LeagueClient.exe", str(running_root / "LeagueClient" / "LeagueClient.exe")),
        ],
    )
    monkeypatch.setattr(lol_locator, "_candidates_from_registry", lambda: [])
    monkeypatch.setattr(lol_locator, "_candidates_from_shortcuts", lambda: [])

    results = lol_locator.detect_lol_installations(
        preferred_path=str(pref_root / "TCLS" / "Client.exe"),
        allow_disk_scan=False,
    )
    assert len(results) == 2
    # Active running client must sort first ahead of preferred non-running install
    assert results[0].install_dir == running_root
    assert results[0].is_running is True
    assert results[1].install_dir == pref_root
    assert results[1].is_running is False

    # Passing preferred_path pointing to same running_root should deduplicate to 1 entry
    deduped = lol_locator.detect_lol_installations(
        preferred_path=str(running_root / "LeagueClient" / "lockfile"),
        allow_disk_scan=False,
    )
    assert len(deduped) == 1
    assert deduped[0].install_dir == running_root
    assert deduped[0].is_running is True


def test_detect_lol_installations_handles_access_denied_elevated_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hex_helper.game import lol_locator

    reg_root = _make_mock_lol_install(tmp_path / "英雄联盟(26)")

    class ElevatedProc:
        pid = 2222
        info = {"pid": 2222, "name": "LeagueClientUx.exe"}

        def exe(self) -> str:
            raise psutil.AccessDenied(pid=2222)

    monkeypatch.setattr(lol_locator.psutil, "process_iter", lambda attrs=None: [ElevatedProc()])
    monkeypatch.setattr(lol_locator, "_candidates_from_registry", lambda: [(reg_root, "registry")])
    monkeypatch.setattr(lol_locator, "_candidates_from_shortcuts", lambda: [])

    results = lol_locator.detect_lol_installations(allow_disk_scan=False)
    assert len(results) == 1
    assert results[0].install_dir == reg_root
    assert results[0].is_running is True
    assert results[0].client_elevated is True


def test_bounded_disk_scan_finds_rail_apps_and_skips_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hex_helper.game import lol_locator

    target_root = _make_mock_lol_install(tmp_path / "rail_apps" / "英雄联盟(26)", with_lockfile=True)
    # Should be skipped due to excluded folder name
    _make_mock_lol_install(tmp_path / "node_modules" / "英雄联盟(99)")
    _make_mock_lol_install(tmp_path / "Windows" / "英雄联盟(98)")
    # Depth 4 (exceeds max_depth=3) should not be scanned
    _make_mock_lol_install(tmp_path / "games" / "tencent" / "wegameapps" / "英雄联盟(97)")

    monkeypatch.setattr(lol_locator.psutil, "process_iter", lambda attrs=None: [])
    monkeypatch.setattr(lol_locator, "_candidates_from_registry", lambda: [])
    monkeypatch.setattr(lol_locator, "_candidates_from_shortcuts", lambda: [])
    monkeypatch.setattr(lol_locator, "_get_fixed_drives", lambda: [tmp_path])

    results = lol_locator.detect_lol_installations(allow_disk_scan=True)
    found_dirs = {r.install_dir for r in results}
    assert target_root in found_dirs
    assert all("99" not in str(d) and "98" not in str(d) and "97" not in str(d) for d in found_dirs)

    primary = lol_locator.detect_primary_lol_install(allow_disk_scan=True)
    assert primary is not None
    assert primary.install_dir == target_root


def test_shortcut_binary_parsing_finds_wegame_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hex_helper.game import lol_locator

    target_root = _make_mock_lol_install(tmp_path / "LOL" / "英雄联盟(26)")
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    lnk = desktop / "英雄联盟WeGame版.lnk"
    exe_str = str(target_root / "WeGameLauncher" / "launcher.exe")
    # Synthetic ShellLink binary blob with both GB18030 and UTF-16LE embedded target paths
    blob = b"\x4c\x00\x00\x00" + b"\x00" * 32 + exe_str.encode("gb18030") + b"\x00\x00" + exe_str.encode("utf-16-le") + b"\x00\x00"
    lnk.write_bytes(blob)

    monkeypatch.setattr(lol_locator.psutil, "process_iter", lambda attrs=None: [])
    monkeypatch.setattr(lol_locator, "_candidates_from_registry", lambda: [])
    monkeypatch.setattr(lol_locator, "_shortcut_search_dirs", lambda: [desktop])

    results = lol_locator.detect_lol_installations(allow_disk_scan=False)
    assert len(results) == 1
    assert results[0].install_dir == target_root
    assert results[0].source == "shortcut"


def test_candidate_lockfile_paths_includes_discovered_lockfile_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hex_helper.game import lockfile, lol_locator

    discovered = _make_mock_lol_install(tmp_path / "英雄联盟(26)", with_lockfile=True)
    info = lol_locator.validate_lol_install_dir(discovered, source="registry")
    assert info is not None

    monkeypatch.setattr(lockfile, "detect_lol_installations", lambda **kwargs: [info])
    monkeypatch.setattr(lockfile, "_read_registry_install_dirs", lambda: [])

    candidates = lockfile.candidate_lockfile_paths()
    assert candidates[0] == discovered / "LeagueClient" / "lockfile"


def test_registry_layer_reads_tencent_wegame_and_uninstall_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hex_helper.game import lol_locator

    lol_root = _make_mock_lol_install(tmp_path / "LOL" / "英雄联盟(26)")
    wegame_dir = tmp_path / "WeGame"
    wegame_dir.mkdir()
    wegame_lol = _make_mock_lol_install(tmp_path / "rail_apps" / "英雄联盟_rail")
    uninstall_lol = _make_mock_lol_install(tmp_path / "Riot" / "League of Legends")

    class FakeKey:
        def __init__(self, values: dict[str, str], subkeys: list[str] | None = None) -> None:
            self._values = values
            self._subkeys = subkeys or []
            self.hive: int = 0
            self.subkey: str = ""

        def __enter__(self) -> FakeKey:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    tree: dict[tuple[int, str], FakeKey] = {
        (1, r"Software\Tencent\LOL"): FakeKey(
            {"InstallPath": str(lol_root), "setup": str(lol_root / "TCLS" / "Client.exe")}
        ),
        (1, r"Software\Tencent\WeGame"): FakeKey({"InstallPath": str(wegame_dir)}),
        (2, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"): FakeKey({}, subkeys=["LoLItem"]),
        (2, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\LoLItem"): FakeKey(
            {
                "DisplayName": "League of Legends (CN)",
                "DisplayIcon": f'"{uninstall_lol / "LeagueClient" / "LeagueClient.exe"}",0',
            }
        ),
    }

    class FakeWinreg:
        HKEY_CURRENT_USER = 1
        HKEY_LOCAL_MACHINE = 2

        @staticmethod
        def OpenKey(hive: int | FakeKey, subkey: str) -> FakeKey:
            lookup = (hive.hive, f"{hive.subkey}\\{subkey}") if isinstance(hive, FakeKey) else (hive, subkey)
            if lookup not in tree:
                raise OSError("not found")
            key = tree[lookup]
            key.hive, key.subkey = lookup
            return key

        @staticmethod
        def QueryValueEx(key: FakeKey, name: str) -> tuple[str, int]:
            if name not in key._values:
                raise OSError("missing value")
            return key._values[name], 1

        @staticmethod
        def EnumKey(key: FakeKey, index: int) -> str:
            if index >= len(key._subkeys):
                raise OSError("no more items")
            return key._subkeys[index]

    monkeypatch.setattr(lol_locator, "winreg", FakeWinreg)
    monkeypatch.setattr(lol_locator.psutil, "process_iter", lambda attrs=None: [])
    monkeypatch.setattr(lol_locator, "_candidates_from_shortcuts", lambda: [])

    results = lol_locator.detect_lol_installations(allow_disk_scan=False)
    found = {r.install_dir for r in results}
    assert lol_root in found
    assert wegame_lol in found
    assert uninstall_lol in found


def test_detect_lol_installations_ttl_cache_and_lockfile_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hex_helper.game import lockfile, lol_locator

    lol_locator.clear_locator_cache()
    reg_root = _make_mock_lol_install(tmp_path / "英雄联盟(26)", with_lockfile=True)

    calls = {"reg": 0}

    def fake_registry() -> list[tuple[Path, str]]:
        calls["reg"] += 1
        return [(reg_root, "registry")]

    monkeypatch.setattr(lol_locator.psutil, "process_iter", lambda attrs=None: [])
    monkeypatch.setattr(lol_locator, "_candidates_from_registry", fake_registry)
    monkeypatch.setattr(lol_locator, "_candidates_from_shortcuts", lambda: [])
    monkeypatch.setattr(lol_locator, "_get_fixed_drives", lambda: [])
    monkeypatch.setattr(lockfile, "_read_registry_install_dirs", lambda: [])

    # Repeated LCU polling via candidate_lockfile_paths uses cache_ttl_s=60.0
    p1 = lockfile.candidate_lockfile_paths()
    p2 = lockfile.candidate_lockfile_paths()
    assert p1[0] == reg_root / "LeagueClient" / "lockfile"
    assert p2[0] == reg_root / "LeagueClient" / "lockfile"
    assert calls["reg"] == 1

    # Clearing cache forces a fresh scan on next call
    lol_locator.clear_locator_cache()
    lockfile.candidate_lockfile_paths()
    assert calls["reg"] == 2
    lol_locator.clear_locator_cache()


def test_disk_scan_short_circuits_when_earlier_layers_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hex_helper.game import lol_locator

    lol_locator.clear_locator_cache()
    reg_root = _make_mock_lol_install(tmp_path / "RegLoL" / "英雄联盟(26)")
    disk_root = _make_mock_lol_install(tmp_path / "rail_apps" / "英雄联盟(99)")

    disk_calls = {"count": 0}

    def fake_drives() -> list[Path]:
        disk_calls["count"] += 1
        return [tmp_path]

    monkeypatch.setattr(lol_locator.psutil, "process_iter", lambda attrs=None: [])
    monkeypatch.setattr(lol_locator, "_candidates_from_registry", lambda: [(reg_root, "registry")])
    monkeypatch.setattr(lol_locator, "_candidates_from_shortcuts", lambda: [])
    monkeypatch.setattr(lol_locator, "_get_fixed_drives", fake_drives)

    res_default = lol_locator.detect_lol_installations(allow_disk_scan=True)
    assert [r.install_dir for r in res_default] == [reg_root]
    assert disk_calls["count"] == 0

    res_forced = lol_locator.detect_lol_installations(allow_disk_scan=True, force_disk_scan=True)
    assert {r.install_dir for r in res_forced} == {reg_root, disk_root}
    assert disk_calls["count"] == 1


def test_shortcut_odd_aligned_utf16_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hex_helper.game import lol_locator

    lol_locator.clear_locator_cache()
    target_root = _make_mock_lol_install(tmp_path / "LOL" / "英雄联盟(26)")
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    lnk = desktop / "英雄联盟Odd.lnk"
    exe_str = str(target_root / "WeGameLauncher" / "launcher.exe")
    # 1-byte odd offset before UTF-16LE string (no ASCII/GB18030 copy)
    blob = b"\x4c" + exe_str.encode("utf-16-le") + b"\x00\x00"
    lnk.write_bytes(blob)

    monkeypatch.setattr(lol_locator.psutil, "process_iter", lambda attrs=None: [])
    monkeypatch.setattr(lol_locator, "_candidates_from_registry", lambda: [])
    monkeypatch.setattr(lol_locator, "_shortcut_search_dirs", lambda: [desktop])

    results = lol_locator.detect_lol_installations(allow_disk_scan=False)
    assert len(results) == 1
    assert results[0].install_dir == target_root


def test_check_vcredist_x64_registry_and_dll_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hex_helper.setup import preflight

    class FakeKey:
        def __init__(self, values: dict[str, object]) -> None:
            self._values = values

        def __enter__(self) -> FakeKey:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    class FakeWinreg:
        HKEY_LOCAL_MACHINE = 2

        def __init__(self, entries: dict[tuple[int, str], FakeKey]) -> None:
            self._entries = entries

        def OpenKey(self, hive: int, subkey: str) -> FakeKey:
            if (hive, subkey) not in self._entries:
                raise OSError("not found")
            return self._entries[(hive, subkey)]

        @staticmethod
        def QueryValueEx(key: FakeKey, name: str) -> tuple[object, int]:
            if name not in key._values:
                raise OSError("missing")
            return key._values[name], 1

    # Case 1: Present in HKLM native x64 VC runtime key
    monkeypatch.setattr(
        preflight,
        "winreg",
        FakeWinreg({
            (2, r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"): FakeKey(
                {"Installed": 1, "Version": "v14.40.33810.00"}
            )
        }),
    )
    ok, detail = preflight.check_vcredist_x64()
    assert ok is True
    assert "v14.40.33810.00" in detail

    # Case 2: Missing from registry, fallback to SystemRoot\System32\vcruntime140.dll
    monkeypatch.setattr(preflight, "winreg", FakeWinreg({}))
    win_root = tmp_path / "Windows"
    (win_root / "System32").mkdir(parents=True)
    (win_root / "System32" / "vcruntime140.dll").write_bytes(b"MZ")
    monkeypatch.setenv("SystemRoot", str(win_root))
    ok_dll, detail_dll = preflight.check_vcredist_x64()
    assert ok_dll is True
    assert "vcruntime140.dll" in detail_dll

    # Case 3: Both registry and DLL missing
    (win_root / "System32" / "vcruntime140.dll").unlink()
    ok_missing, detail_missing = preflight.check_vcredist_x64()
    assert ok_missing is False
    assert "VC++" in detail_missing


def test_check_winrt_zh_ocr_supported_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types
    from hex_helper.setup import preflight

    fake_glob = types.ModuleType("winrt.windows.globalization")
    fake_ocr = types.ModuleType("winrt.windows.media.ocr")

    class FakeLanguage:
        def __init__(self, tag: str) -> None:
            self.language_tag = tag

    class FakeOcrEngineSupported:
        @staticmethod
        def is_language_supported(lang: FakeLanguage) -> bool:
            return lang.language_tag == "zh-Hans-CN"

    fake_glob.Language = FakeLanguage  # type: ignore[attr-defined]
    fake_ocr.OcrEngine = FakeOcrEngineSupported  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "winrt.windows.globalization", fake_glob)
    monkeypatch.setitem(sys.modules, "winrt.windows.media.ocr", fake_ocr)

    ok, detail = preflight.check_winrt_zh_ocr()
    assert ok is True
    assert "zh-Hans-CN" in detail

    class FakeOcrEngineMissing:
        @staticmethod
        def is_language_supported(_lang: FakeLanguage) -> bool:
            return False

    fake_ocr.OcrEngine = FakeOcrEngineMissing  # type: ignore[attr-defined]
    ok_miss, detail_miss = preflight.check_winrt_zh_ocr()
    assert ok_miss is False
    assert "zh-Hans-CN" in detail_miss or "简体中文" in detail_miss


def test_extract_bundled_seed_cache_never_clobbers_existing(tmp_path: Path) -> None:
    from hex_helper.setup.preflight import extract_bundled_seed_cache

    seed_dir = tmp_path / "bundled_seed"
    ds_seed = seed_dir / "datasets" / "16.23" / "normal"
    ds_seed.mkdir(parents=True)
    (ds_seed / "heroes.json").write_text('{"seed": true}', encoding="utf-8")
    (ds_seed / "hero_augments.json").write_text('{"aug": 1}', encoding="utf-8")
    (seed_dir / "static").mkdir(parents=True)
    (seed_dir / "static" / "ddragon_items.json").write_text('{"items": 1}', encoding="utf-8")

    data_dir = tmp_path / "appdata"
    ds_cache = data_dir / "cache" / "datasets" / "16.23" / "normal"
    ds_cache.mkdir(parents=True)
    # Existing non-empty cache must NOT be overwritten
    (ds_cache / "heroes.json").write_text('{"newer": true}', encoding="utf-8")
    # Existing 0-byte file MUST be replaced from seed
    (data_dir / "cache" / "static").mkdir(parents=True)
    (data_dir / "cache" / "static" / "ddragon_items.json").write_bytes(b"")

    assert extract_bundled_seed_cache(data_dir, seed_dir_override=seed_dir) is True
    assert (ds_cache / "heroes.json").read_text(encoding="utf-8") == '{"newer": true}'
    assert (ds_cache / "hero_augments.json").read_text(encoding="utf-8") == '{"aug": 1}'
    assert (data_dir / "cache" / "static" / "ddragon_items.json").read_text(encoding="utf-8") == '{"items": 1}'

    # Missing seed dir returns False cleanly
    assert extract_bundled_seed_cache(data_dir, seed_dir_override=tmp_path / "nonexistent") is False


def test_run_windows_preflight_populates_config_and_report_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from hex_helper.config import AppConfig
    from hex_helper.game.lol_locator import LolInstallInfo
    from hex_helper.setup import preflight

    seed_dir = tmp_path / "bundled_seed"
    ds_seed = seed_dir / "datasets" / "16.23" / "normal"
    ds_seed.mkdir(parents=True)
    (ds_seed / "heroes.json").write_text('{"1": {}}', encoding="utf-8")
    (ds_seed / "hero_augments.json").write_text('{"1": {}}', encoding="utf-8")

    lol_root = _make_mock_lol_install(tmp_path / "英雄联盟(26)")
    fake_lol = LolInstallInfo(
        install_dir=lol_root,
        league_client_dir=lol_root / "LeagueClient",
        lockfile_path=lol_root / "LeagueClient" / "lockfile",
        source="registry",
    )

    monkeypatch.setattr(preflight, "check_vcredist_x64", lambda: (True, "VC++ x64 ready"))
    monkeypatch.setattr(preflight, "check_winrt_zh_ocr", lambda: (True, "OCR ready"))
    monkeypatch.setattr(preflight, "detect_primary_lol_install", lambda **kwargs: fake_lol)
    monkeypatch.setenv("HEX_EDITION", "commercial")

    cfg = AppConfig()
    data_dir = tmp_path / "data"
    report = preflight.run_windows_preflight(cfg, data_dir, seed_dir_override=seed_dir)

    assert report.all_critical_ok is True
    assert cfg.lockfile_path == str(fake_lol.lockfile_path)
    assert len(report.items) == 4
    keys = {it.key for it in report.items}
    assert keys == {"vcredist", "winrt_ocr", "seed_data", "lol_path"}
    assert all(it.ok for it in report.items)

    d = report.to_dict()
    assert d["edition"] == "commercial"
    assert d["all_critical_ok"] is True
    assert d["lol_install_dir"] == str(lol_root)
    assert d["lockfile_path"] == str(fake_lol.lockfile_path)
    roundtrip = json.loads(json.dumps(d, ensure_ascii=False))
    assert len(roundtrip["items"]) == 4

    # When WinRT OCR fails, all_critical_ok must be False
    monkeypatch.setattr(preflight, "check_winrt_zh_ocr", lambda: (False, "OCR missing"))
    bad_report = preflight.run_windows_preflight(AppConfig(), data_dir, seed_dir_override=seed_dir)
    assert bad_report.all_critical_ok is False


def test_setup_wizard_dialog_rescan_manual_lol_and_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QApplication
    from hex_helper.config import AppConfig, load_config
    from hex_helper.game.lol_locator import LolInstallInfo
    from hex_helper.setup.preflight import PreflightItem, PreflightReport
    from hex_helper.ui.setup_wizard import SetupWizardDialog

    app = QApplication.instance() or QApplication([])
    data_dir = tmp_path / "appdata"
    data_dir.mkdir(parents=True)
    cfg = AppConfig()

    initial_report = PreflightReport(
        items=[
            PreflightItem("vcredist", "VC++ x64 运行库", True, "OK"),
            PreflightItem("winrt_ocr", "Windows 中文 OCR 引擎", False, "未安装", "补装中文 OCR"),
            PreflightItem("seed_data", "26.23 离线数据缓存", True, "OK"),
            PreflightItem("lol_path", "英雄联盟客户端路径", False, "未检测到", "手动选择", critical=False),
        ],
        lol_install=None,
    )

    wizard = SetupWizardDialog(cfg, initial_report, data_dir)
    assert wizard.install_ocr_btn.isHidden() is False
    assert wizard.selected_lockfile_path == ""

    # Invalid directory fails validation cleanly
    invalid_dir = tmp_path / "not_lol"
    invalid_dir.mkdir()
    assert wizard.apply_manual_lol_dir(invalid_dir) is False
    assert wizard.selected_lockfile_path == ""

    # Valid directory updates lockfile path and lol_path status
    valid_lol = _make_mock_lol_install(tmp_path / "英雄联盟(26)")
    assert wizard.apply_manual_lol_dir(valid_lol) is True
    expected_lf = str(valid_lol / "LeagueClient" / "lockfile")
    assert wizard.selected_lockfile_path == expected_lf
    assert cfg.lockfile_path == expected_lf

    # Rescan updates items & hides OCR install button once fixed
    rescanned_lol = LolInstallInfo(valid_lol, valid_lol / "LeagueClient", valid_lol / "LeagueClient" / "lockfile", "scan")
    fixed_report = PreflightReport(
        items=[
            PreflightItem("vcredist", "VC++ x64 运行库", True, "OK"),
            PreflightItem("winrt_ocr", "Windows 中文 OCR 引擎", True, "就绪"),
            PreflightItem("seed_data", "26.23 离线数据缓存", True, "OK"),
            PreflightItem("lol_path", "英雄联盟客户端路径", True, f"已定位: {valid_lol}", critical=False),
        ],
        lol_install=rescanned_lol,
    )
    monkeypatch.setattr("hex_helper.ui.setup_wizard.run_windows_preflight", lambda *_a, **_kw: fixed_report)
    wizard.rescan()
    assert wizard.install_ocr_btn.isHidden() is True

    # Clicking save persists config.json in data_dir
    wizard.save_btn.click()
    saved = load_config(data_dir / "config.json")
    assert saved.lockfile_path == expected_lf
    wizard.close()
    app.processEvents()


def test_main_preflight_check_only_outputs_json_and_report_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json
    from hex_helper import main as hex_main
    from hex_helper.config import load_config
    from hex_helper.game.lol_locator import LolInstallInfo
    from hex_helper.setup import preflight

    data_dir = tmp_path / "appdata"
    seed_dir = tmp_path / "bundled_seed"
    ds_seed = seed_dir / "datasets" / "16.23" / "normal"
    ds_seed.mkdir(parents=True)
    (ds_seed / "heroes.json").write_text('{"1": {}}', encoding="utf-8")
    (ds_seed / "hero_augments.json").write_text('{"1": {}}', encoding="utf-8")

    lol_root = _make_mock_lol_install(tmp_path / "英雄联盟(26)")
    fake_lol = LolInstallInfo(
        install_dir=lol_root,
        league_client_dir=lol_root / "LeagueClient",
        lockfile_path=lol_root / "LeagueClient" / "lockfile",
        source="registry",
    )

    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("HEX_EDITION", "commercial")
    monkeypatch.setattr(preflight, "check_vcredist_x64", lambda: (True, "VC ok"))
    monkeypatch.setattr(preflight, "check_winrt_zh_ocr", lambda: (True, "OCR ok"))
    monkeypatch.setattr(preflight, "detect_primary_lol_install", lambda **kwargs: fake_lol)
    monkeypatch.setattr(preflight, "get_bundle_root", lambda: tmp_path)

    report_out = tmp_path / "out" / "preflight_report.json"
    rc = hex_main.main(["--preflight-check-only", "--preflight-report-out", str(report_out)])
    assert rc == 0

    captured = capsys.readouterr()
    stdout_data = json.loads(captured.out)
    file_data = json.loads(report_out.read_text(encoding="utf-8"))
    assert stdout_data == file_data
    assert file_data["edition"] == "commercial"
    assert file_data["all_critical_ok"] is True
    assert file_data["lockfile_path"] == str(fake_lol.lockfile_path)

    persisted_cfg = load_config(data_dir / "config.json")
    assert persisted_cfg.lockfile_path == str(fake_lol.lockfile_path)


def test_write_uninstall_script_deletes_spaced_dir_via_real_cmd(tmp_path: Path) -> None:
    import importlib.util
    import subprocess
    import time

    repo_root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("hex_installer_gui", repo_root / "packaging" / "installer_gui.py")
    assert spec is not None and spec.loader is not None
    installer_gui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer_gui)

    install_dir = tmp_path / "Program Files" / "Hex Helper (Test)"
    install_dir.mkdir(parents=True)
    (install_dir / "HexHelper.exe").write_bytes(b"MZ")
    fake_desktop_lnk = tmp_path / "Desktop" / "HexHelper.lnk"
    fake_desktop_lnk.parent.mkdir(parents=True)
    fake_desktop_lnk.write_bytes(b"LNK")
    fake_sm_dir = tmp_path / "StartMenu" / "HexHelper"
    fake_sm_dir.mkdir(parents=True)
    (fake_sm_dir / "HexHelper.lnk").write_bytes(b"LNK")

    uninstall_cmd = installer_gui._write_uninstall_script(
        install_dir,
        desktop_lnk=fake_desktop_lnk,
        start_menu_dir=fake_sm_dir,
        remove_registry=False,
    )
    assert uninstall_cmd.is_file()
    cmd_text = uninstall_cmd.read_text(encoding="utf-8")
    assert 'cd /d "%TEMP%"' in cmd_text
    assert '\\"' not in cmd_text

    # Execute from INSIDE install_dir to prove cd /d "%TEMP%" releases directory lock
    subprocess.run(["cmd.exe", "/c", str(uninstall_cmd)], cwd=str(install_dir), check=True)
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and install_dir.exists():
        time.sleep(0.2)

    assert not install_dir.exists()
    assert not fake_desktop_lnk.exists()
    assert not fake_sm_dir.exists()

    with pytest.raises(ValueError):
        installer_gui._write_uninstall_script(tmp_path / 'bad"dir', remove_registry=False)


def test_patch_edition_text_validates_anchors_and_patches_values() -> None:
    import importlib.util

    repo_root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("build_windows_release", repo_root / "scripts" / "build_windows_release.py")
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    sample = (
        'APP_VERSION = "1.0.0"\n'
        'APP_NAME = "HexHelper"\n'
        '_COMPILED_EDITION = "dev"\n'
        'DEFAULT_CLOUD_API_BASE: str = os.environ.get(\n'
        '    "HEX_CLOUD_API_BASE",\n'
        '    "https://api.hexhelper.example.com",\n'
        ').rstrip("/")\n'
    )
    patched = builder._patch_edition_text(
        sample,
        version="1.0.1",
        edition="commercial",
        cloud_api_base="https://cloud.hexhelper.cn/",
    )
    assert 'APP_VERSION = "1.0.1"' in patched
    assert '_COMPILED_EDITION = "commercial"' in patched
    assert '"https://cloud.hexhelper.cn"' in patched

    with pytest.raises(RuntimeError, match="APP_VERSION"):
        builder._patch_edition_text('_COMPILED_EDITION = "dev"\n', version="1.0.1", edition="commercial")

    with pytest.raises(RuntimeError, match="_COMPILED_EDITION"):
        builder._patch_edition_text('APP_VERSION = "1.0.0"\n', version="1.0.1", edition="commercial")

    with pytest.raises(RuntimeError, match="HEX_CLOUD_API_BASE"):
        builder._patch_edition_text(
            'APP_VERSION = "1.0.0"\n_COMPILED_EDITION = "dev"\n',
            version="1.0.1",
            edition="commercial",
            cloud_api_base="https://cloud.hexhelper.cn",
        )


def test_find_cached_dataset_label_sorts_patches_numerically(tmp_path: Path) -> None:
    from hex_helper.setup.preflight import _find_cached_dataset_label

    cache_dir = tmp_path / "cache"
    for patch, mode in (("16.9", "normal"), ("16.23", "first-prismatic"), ("16.23", "normal")):
        d = cache_dir / "datasets" / patch / mode
        d.mkdir(parents=True)
        (d / "heroes.json").write_text("{}", encoding="utf-8")
        (d / "hero_augments.json").write_text("{}", encoding="utf-8")

    # 16.23 must outrank 16.9 numerically (even though "16.9" > "16.23" lexicographically), and normal > first-prismatic
    assert _find_cached_dataset_label(cache_dir) == "16.23/normal"
