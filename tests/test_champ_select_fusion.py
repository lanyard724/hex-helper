"""Tests for ChampSelectOverlay dual mode toggle, swap-gain badges, Best-5 strip, and app persistence."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from hex_helper.config import AppConfig
from hex_helper.data.provider import CoreBuild, HeroStat, ItemStaticInfo
from hex_helper.game.models import ChampSelectState
from hex_helper.ui.champ_select_ui import (
    ChampSelectOverlay,
    analyze_team_needs,
    summarize_team_composition,
)


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _DummyStatic:
    def __init__(self, tmp_path: Path) -> None:
        self.icons_dir = tmp_path

    def ensure_icon(self, *_args, **_kwargs):
        return None

    def get_spell_name(self, sid: str) -> str:
        return {"4": "闪现", "32": "雪球"}.get(sid, sid)


class _DummyStore:
    def __init__(self, tmp_path: Path) -> None:
        self.static = _DummyStatic(tmp_path)
        self.descriptor = None
        self._heroes = {
            "1": HeroStat("1", "拉克丝", "", 10000, 5100, 0.51, 0.05, 10),
            "2": HeroStat("2", "泽拉斯", "", 10000, 5200, 0.52, 0.05, 8),
            "3": HeroStat("3", "吉格斯", "", 10000, 5100, 0.51, 0.05, 11),
            "4": HeroStat("4", "维克托", "", 10000, 5000, 0.50, 0.05, 20),
            "5": HeroStat("5", "辛德拉", "", 10000, 4900, 0.49, 0.05, 35),
            "6": HeroStat("6", "诺提勒斯", "", 10000, 5300, 0.53, 0.05, 5),
            "236": HeroStat("236", "卢锡安", "", 10000, 5150, 0.515, 0.05, 18),
        }

    def get_hero(self, hid: str):
        return self._heroes.get(str(hid))

    def get_core_builds(self, hid: str):
        if str(hid) == "6":
            iid = "tank"
        elif str(hid) == "236":
            iid = "ad"
        else:
            iid = "ap"
        return [CoreBuild((iid,), True, 5000, 2600, 0.52, 0.3)]

    def get_item_info(self, iid: str):
        if iid == "tank":
            return ItemStaticInfo(iid, "心之钢", 3000, stats={"FlatHPPoolMod": 900.0, "FlatArmorMod": 60.0})
        if iid == "ad":
            return ItemStaticInfo(iid, "无尽之刃", 3400, stats={"FlatPhysicalDamageMod": 75.0, "FlatCritChanceMod": 0.25})
        return ItemStaticInfo(iid, "卢登", 2850, stats={"FlatMagicDamageMod": 100.0})

    def get_hero_spells(self, _hid: str):
        return []


def test_champ_select_renders_swap_gain_best_five_and_toggles_goal_mode(tmp_path: Path) -> None:
    app = _qt_app()
    seen_modes: list[str] = []
    ov = ChampSelectOverlay(on_goal_mode_changed=seen_modes.append)
    store = _DummyStore(tmp_path)
    state = ChampSelectState(
        queue_id=2400,
        game_mode="KIWI",
        is_mayhem=True,
        my_hero_id="5",
        team_hero_ids=("1", "2", "3", "4", "5"),
        bench_hero_ids=("6",),
    )
    ov.update_state(state, store)  # type: ignore[arg-type]
    app.processEvents()

    # Unified action bar recommends concrete swap instead of static-only warning
    assert not ov.comp_alert_lbl.isHidden()
    assert "诺提勒斯" in ov.comp_alert_lbl.text()
    assert "建议换" in ov.comp_alert_lbl.text()

    # Bench row preserves [补前排] gap badge AND shows positive swap-gain pill alongside it
    row_texts = [lbl.text() for lbl in ov.rows_body.findChildren(QLabel)]
    assert any("[补前排]" in t for t in row_texts)
    assert any(t.startswith("+") and "分" in t for t in row_texts)

    # Best-5 strip is visible, collapsed by default, and expands inline on toggle
    assert not ov.best_five_strip.isHidden()
    assert "6 阵" in ov.best_five_hdr_lbl.text()
    assert ov.best_five_body.isHidden()
    collapsed_h = ov.height()

    ov.best_five_toggle_btn.click()
    app.processEvents()
    assert not ov.best_five_body.isHidden()
    assert ov.height() > collapsed_h
    b5_texts = [lbl.text() for lbl in ov.best_five_body.findChildren(QLabel)]
    assert any("诺提勒斯" in t for t in b5_texts)
    assert "冲分最优" in ov.best_five_tags_lbl.text()

    # Goal mode switching via API & segmented pill buttons
    assert ov.goal_mode == "climb"
    ov.set_goal_mode("fun")
    app.processEvents()
    assert seen_modes == ["fun"]
    assert ov.goal_mode == "fun"
    assert ov.mode_fun_btn.isChecked()
    assert not ov.mode_climb_btn.isChecked()
    assert "爽局高上限" in ov.best_five_tags_lbl.text()
    assert "爽局" in ov.team_score_lbl.text()

    # Clicking the already-active fun pill keeps it checked and emits no duplicate callback
    ov.mode_fun_btn.click()
    app.processEvents()
    assert seen_modes == ["fun"]
    assert ov.mode_fun_btn.isChecked()

    # Collapse back cleanly
    ov.best_five_toggle_btn.click()
    app.processEvents()
    assert ov.best_five_body.isHidden()

    # Clicking climb pill switches back and notifies callback
    ov.mode_climb_btn.click()
    app.processEvents()
    assert seen_modes == ["fun", "climb"]
    assert ov.goal_mode == "climb"
    assert ov.mode_climb_btn.isChecked()

    # Desktop Hextech visual isolation: zero Web Tactile Orange (#FF5A1F)
    all_qss = ov.styleSheet() + " ".join(w.styleSheet() for w in ov.findChildren(QWidget))
    assert "#FF5A1F" not in all_qss.upper()

    ov.close()
    app.processEvents()


def test_champ_select_sorts_bench_by_swap_gain_and_renders_non_gap_gain_reason(tmp_path: Path) -> None:
    app = _qt_app()
    store = _DummyStore(tmp_path)
    # Add a balanced frontline & high-winrate non-gap bench carry
    store._heroes["10"] = HeroStat("10", "布隆", "", 10000, 5100, 0.51, 0.05, 22)
    store._heroes["11"] = HeroStat("11", "金克丝", "", 10000, 5600, 0.56, 0.05, 1)
    orig_builds = store.get_core_builds

    def _custom_builds(hid: str):
        if str(hid) == "10":
            return [CoreBuild(("tank",), True, 5000, 2600, 0.52, 0.3)]
        if str(hid) == "11":
            return [CoreBuild(("ad",), True, 5000, 2800, 0.56, 0.3)]
        return orig_builds(hid)

    store.get_core_builds = _custom_builds  # type: ignore[method-assign]

    ov = ChampSelectOverlay()
    # 1) All-AP team with bench=("236", "6"): TANK ("6" 诺提勒斯) has higher swap gain than AD ("236" 卢锡安)
    ov.update_state(
        ChampSelectState(2400, "KIWI", True, "5", ("1", "2", "3", "4", "5"), ("236", "6")),
        store,  # type: ignore[arg-type]
    )
    app.processEvents()
    row_texts = [lbl.text() for lbl in ov.rows_body.findChildren(QLabel)]
    idx_naut = next(i for i, t in enumerate(row_texts) if t == "诺提勒斯")
    idx_lucian = next(i for i, t in enumerate(row_texts) if t == "卢锡安")
    assert idx_naut < idx_lucian

    # 2) Balanced team (2AP + 2AD + 1TANK) with a strong bench AD carry ("11" 金克丝) -> non-gap swap-gain badge
    ov.update_state(
        ChampSelectState(2400, "KIWI", True, "5", ("1", "2", "236", "10", "5"), ("11",)),
        store,  # type: ignore[arg-type]
    )
    app.processEvents()
    row_texts_bal = [lbl.text() for lbl in ov.rows_body.findChildren(QLabel)]
    assert any(t.startswith("+") and "分 · " in t for t in row_texts_bal)
    assert not any("[补前排]" in t or "[补AD]" in t or "[补AP]" in t for t in row_texts_bal)

    ov.close()
    app.processEvents()


def test_champ_select_hides_best_five_for_small_pool_and_preserves_legacy_exports(tmp_path: Path) -> None:
    app = _qt_app()
    store = _DummyStore(tmp_path)

    # Legacy exports remain intact
    msg, is_warn, needed = analyze_team_needs(("1", "2", "3", "4"), store)  # type: ignore[arg-type]
    assert is_warn is True
    assert "TANK" in needed and "AD" in needed
    sum_msg, sum_warn = summarize_team_composition(("1", "2", "3", "4"), store)  # type: ignore[arg-type]
    assert sum_msg == msg and sum_warn is True

    ov = ChampSelectOverlay(initial_goal_mode="fun")
    assert ov.goal_mode == "fun"
    assert ov.mode_fun_btn.isChecked()

    # Pool with < 5 heroes hides Best-5 strip and < 3 heroes hides comp alert
    ov.update_state(
        ChampSelectState(2400, "KIWI", True, "5", ("5", "1"), ()),
        store,  # type: ignore[arg-type]
    )
    app.processEvents()
    assert ov.best_five_strip.isHidden()
    assert ov.comp_alert_lbl.isHidden()

    # Pool with 5 allies + 2 bench = 7 heroes -> C(7,5) = 21 combos, sorts highest swap gain first
    ov.update_state(
        ChampSelectState(2400, "KIWI", True, "5", ("1", "2", "3", "4", "5"), ("236", "6")),
        store,  # type: ignore[arg-type]
    )
    app.processEvents()
    assert not ov.best_five_strip.isHidden()
    assert "21 阵" in ov.best_five_hdr_lbl.text()

    ov.close()
    app.processEvents()


def test_app_persists_champ_select_goal_mode(monkeypatch) -> None:
    from hex_helper import app as app_mod

    saved: list[str] = []
    monkeypatch.setattr(app_mod, "save_config", lambda cfg: saved.append(cfg.champ_select_goal_mode))

    dummy = object.__new__(app_mod.HexHelperController)
    dummy.cfg = AppConfig(champ_select_goal_mode="climb")

    dummy._on_champ_goal_mode_changed("fun")
    assert dummy.cfg.champ_select_goal_mode == "fun"
    assert saved == ["fun"]

    # Repeated same mode does not trigger redundant disk write
    dummy._on_champ_goal_mode_changed("fun")
    assert saved == ["fun"]

    # Invalid value safely normalizes to "climb"
    dummy._on_champ_goal_mode_changed("unknown")
    assert dummy.cfg.champ_select_goal_mode == "climb"
    assert saved == ["fun", "climb"]


def _write_dummy_portrait_png(icons_dir: Path, hid: str) -> Path:
    champ_dir = icons_dir / "champion"
    champ_dir.mkdir(parents=True, exist_ok=True)
    out_path = champ_dir / f"{hid}.png"
    img = QImage(48, 48, QImage.Format.Format_RGBA8888)
    seed = int(hid) * 73 + 19
    for y in range(48):
        for x in range(48):
            v = ((x * 37 + y * 91 + seed) ^ (x * y * 13 + seed)) & 0xFF
            img.setPixelColor(x, y, QColor(v, (v * 3 + x * 7) & 0xFF, (v * 5 + y * 11) & 0xFF))
    assert img.save(str(out_path), "PNG")
    assert out_path.stat().st_size > 200
    return out_path


def _count_valid_pixmaps(container: QWidget) -> int:
    return sum(
        1
        for lbl in container.findChildren(QLabel)
        if not lbl.pixmap().isNull()
    )


def _best_five_chip_ids(ov: ChampSelectOverlay) -> list[int]:
    return [
        id(ov.best_five_heroes_lay.itemAt(i).widget())
        for i in range(ov.best_five_heroes_lay.count())
    ]


def test_best_five_refreshes_portraits_after_async_disk_cache_and_avoids_warm_rebuild(tmp_path: Path) -> None:
    app = _qt_app()
    store = _DummyStore(tmp_path)
    # 5 allies only -> _build_hero_row uses #F0E6D2 / #785A28 while best_five uses #C8AA6E,
    # directly exercising cold-cache async disk arrival where cache keys differ by border color.
    allies_state = ChampSelectState(
        queue_id=2400,
        game_mode="KIWI",
        is_mayhem=True,
        my_hero_id="5",
        team_hero_ids=("1", "2", "3", "4", "5"),
        bench_hero_ids=(),
    )

    # 1. Cold disk cache: first render falls back to text initials in best_five_body
    ov = ChampSelectOverlay()
    ov.update_state(allies_state, store)  # type: ignore[arg-type]
    app.processEvents()
    assert _count_valid_pixmaps(ov.best_five_body) == 0

    # Simulate async _fetch thread writing champion PNGs to disk (without populating _icon_cache)
    for hid in ("1", "2", "3", "4", "5", "6"):
        _write_dummy_portrait_png(tmp_path, hid)

    # Subsequent refresh (_refresh_last_state / LCU poll) MUST upgrade all 5 best_five chips to real pixmaps
    ov.update_state(allies_state, store)  # type: ignore[arg-type]
    app.processEvents()
    assert _count_valid_pixmaps(ov.rows_body) == 5
    assert _count_valid_pixmaps(ov.best_five_body) == 5

    # Next unchanged poll MUST NOT destroy/recreate best_five chips
    chip_ids_after_load = _best_five_chip_ids(ov)
    assert len(chip_ids_after_load) == 5
    ov.update_state(allies_state, store)  # type: ignore[arg-type]
    app.processEvents()
    assert _best_five_chip_ids(ov) == chip_ids_after_load
    ov.close()
    app.processEvents()

    # 2. Warm disk cache (PNGs already on disk, fresh overlay with empty _icon_cache, including bench):
    # First render loads all 5 pixmaps immediately, and second poll skips redundant chip rebuild
    with_bench_state = ChampSelectState(
        queue_id=2400,
        game_mode="KIWI",
        is_mayhem=True,
        my_hero_id="5",
        team_hero_ids=("1", "2", "3", "4", "5"),
        bench_hero_ids=("6",),
    )
    ov2 = ChampSelectOverlay()
    ov2.update_state(with_bench_state, store)  # type: ignore[arg-type]
    app.processEvents()
    assert _count_valid_pixmaps(ov2.best_five_body) == 5
    warm_chip_ids = _best_five_chip_ids(ov2)
    assert len(warm_chip_ids) == 5

    ov2.update_state(with_bench_state, store)  # type: ignore[arg-type]
    app.processEvents()
    assert _best_five_chip_ids(ov2) == warm_chip_ids
    ov2.close()
    app.processEvents()
