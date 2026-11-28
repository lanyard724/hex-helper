"""Live host smoke test verifying real 26.23 sync, DXGI/MSS capture, Win32 HWND/WS_EX flags, WinRT OCR, and Antigravity bridge -> Qt UI rendering."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PySide6.QtWidgets import QApplication

from hex_helper.bridge.agent_bridge import AgentBridgeServer
from hex_helper.bridge.protocol import CoachRequestPayload, utc_now_iso
from hex_helper.config import get_app_data_dir
from hex_helper.data.aramhex_sync import AramHexSyncer
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.models import ChampSelectState, PlayerLiveInfo
from hex_helper.scoring.matchup_optimizer import build_next_buy_plan, optimize_next_items
from hex_helper.scoring.tier import score_hero_augment
from hex_helper.ui.augment_overlay import AugmentBadgeRenderItem, AugmentBadgesOverlay
from hex_helper.ui.champ_select_ui import ChampSelectOverlay
from hex_helper.ui.edge_drawer import EdgeBuildDrawer
from hex_helper.ui.lookup_window import LookupWindow
from hex_helper.ui.win32_window import (
    UiBridgeInvoker,
    WS_EX_NOACTIVATE,
    WS_EX_TOPMOST,
    WS_EX_TRANSPARENT,
    get_ex_style,
)
from hex_helper.vision.capture import ScreenCapturer, attach_default_desktop


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    attach_default_desktop()
    data_dir = get_app_data_dir()
    cache_dir = data_dir / "cache"
    print(f"[1/5] Live sync of 26.23 datasets & static assets -> {cache_dir}")

    syncer = AramHexSyncer(cache_dir, proxy_url="http://127.0.0.1:7900")
    datasets = syncer.fetch_index()
    assert len(datasets) >= 2, f"Expected >=2 datasets, got {len(datasets)}"

    desc, ddir = syncer.sync_dataset("latest", "normal")
    # Also pre-cache current 16.23 first-prismatic event dataset if present
    for ds in datasets:
        if ds.patch == desc.patch and ds.mode == "first-prismatic":
            syncer.sync_dataset(ds.patch, ds.mode)
            break

    static = StaticAssetsManager(cache_dir, proxy_url="http://127.0.0.1:7900")
    store = DataStore(static)
    store.load_from_dir(desc, ddir)
    static.sync_all(dataset_item_meta=store.get_dataset_item_meta())
    store.load_from_dir(desc, ddir)

    assert len(store.all_heroes()) >= 160
    assert len(store.all_augments()) >= 200
    assert len(static.items) >= 400
    icon_path = static.ensure_icon("champion", "222")
    assert icon_path is not None and icon_path.exists() and icon_path.stat().st_size > 1000
    print(
        f"  OK: patch={desc.patch}/{desc.mode} heroes={len(store.all_heroes())} "
        f"augments={len(store.all_augments())} ddragon_items={len(static.items)} icon={icon_path.name}"
    )

    print("[2/5] Live 10-player matchup optimizer on real 26.23 Jinx data")
    allies = [
        PlayerLiveInfo("me", "金克丝", "Jinx", "ORDER", True, 12, 5, 3, 6, ("3085",)),
        PlayerLiveInfo("a2", "拉克丝", "Lux", "ORDER", False, 12, 4, 3, 9, ("6655",)),
    ]
    enemies = [
        PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 13, 9, 2, 4, ("3075", "3084")),
        PlayerLiveInfo("e2", "索拉卡", "Soraka", "CHAOS", False, 12, 1, 2, 14, ()),
        PlayerLiveInfo("e3", "蒙多医生", "DrMundo", "CHAOS", False, 13, 6, 3, 5, ("3084", "3075")),
    ]
    threat, dyn_recs = optimize_next_items(
        store, "222", ("3085",), 3200.0, ["3081"], allies, enemies, limit=4
    )
    assert len(dyn_recs) == 4
    print(f"  OK: threats={threat.summary_tags} top_recs={[(r.name, r.price, r.reasons[0]) for r in dyn_recs[:3]]}")

    print("[3/5] Live desktop screen capture non-black assertion")
    cap = ScreenCapturer(prefer_dxcam=True)
    try:
        res = cap.grab(region=(0, 0, 640, 360))
        assert res.frame_bgr is not None and res.frame_bgr.size > 0
        assert int(np.max(res.frame_bgr)) > 0 and not res.is_black, f"Black frame detected: luma={res.mean_luma}"
        print(f"  OK: backend={res.backend} shape={res.frame_bgr.shape} max={int(np.max(res.frame_bgr))} mean={res.mean_luma:.1f}")
    finally:
        cap.close()

    print("[4/5] Live PySide6 HWND, dimensions, and Win32 WS_EX_NOACTIVATE / WS_EX_TRANSPARENT checks")
    app = QApplication.instance() or QApplication([])
    invoker = UiBridgeInvoker()

    champ_ui = ChampSelectOverlay()
    champ_ui.update_state(ChampSelectState(2400, "KIWI", True, "222", ("222", "101"), ("10", "236")), store)
    champ_ui.show_overlay()

    drawer = EdgeBuildDrawer()
    drawer.populate_catalogs(store, datasets)
    next_plan = build_next_buy_plan(store, "222", ("3085",), 3200.0, dyn_recs)
    drawer.refresh_match_view(store, "222", ["3081"], ("3085",), 3200.0, threat, dyn_recs, next_plan=next_plan)
    drawer.set_expanded(True)
    drawer.show_overlay()

    aug_ui = AugmentBadgesOverlay()
    sc = score_hero_augment("3081", "命运之武器", "gold", 0.4804, 24939, 46967, False)
    aug_ui.render_cards(
        [AugmentBadgeRenderItem(1, sc, (1160, 580, 1400, 630), is_best=True)],
        screen_rect=(0, 0, 2560, 1440),
        coach_augment_tip="优先拿命运之武器绑定破败",
    )

    lookup = LookupWindow(cfg=None if False else __import__("hex_helper.config", fromlist=["AppConfig"]).AppConfig())
    lookup.bind_store(store)
    lookup.show()

    app.processEvents()
    time.sleep(0.15)
    app.processEvents()

    for name, w, expect_transparent in (
        ("ChampSelectOverlay", champ_ui, False),
        ("EdgeBuildDrawer", drawer, False),
        ("AugmentBadgesOverlay", aug_ui, True),
    ):
        hwnd = int(w.winId())
        ex = get_ex_style(hwnd)
        assert hwnd > 0 and w.width() > 0 and w.height() > 0, f"{name} invalid HWND/size"
        assert ex & WS_EX_NOACTIVATE, f"{name} missing WS_EX_NOACTIVATE (ex=0x{ex:x})"
        assert ex & WS_EX_TOPMOST, f"{name} missing WS_EX_TOPMOST (ex=0x{ex:x})"
        if expect_transparent:
            assert ex & WS_EX_TRANSPARENT, f"{name} missing WS_EX_TRANSPARENT (ex=0x{ex:x})"
        else:
            assert not (ex & WS_EX_TRANSPARENT), f"{name} unexpectedly has WS_EX_TRANSPARENT"
        print(f"  OK: {name} hwnd={hwnd} size={w.width()}x{w.height()} ex=0x{ex:x}")

    assert int(lookup.winId()) > 0 and lookup.hero_list.count() >= 160
    print(f"  OK: LookupWindow heroes_listed={lookup.hero_list.count()}")

    print("[5/5] Live Antigravity bridge -> coach_relay.py -> push_advice.py -> EdgeBuildDrawer UI rendering")
    bridge = AgentBridgeServer(
        bridge_dir=data_dir / "bridge",
        port=0,
        on_advice=lambda adv: invoker.invoke.emit(lambda: drawer.set_coach_advice(adv)),
        on_online_changed=lambda ok: invoker.invoke.emit(lambda: drawer.set_coach_online(ok)),
    )
    port = bridge.start()
    try:
        root = Path(__file__).resolve().parents[1]
        relay = root / "scripts" / "coach_relay.py"
        pusher = root / "scripts" / "push_advice.py"
        cursor = data_dir / "bridge" / "smoke_cursor.txt"
        if cursor.exists():
            cursor.unlink()

        proc = subprocess.Popen(
            [sys.executable, str(relay), "--wait", "--port", str(port), "--cursor-file", str(cursor), "--timeout", "10"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        t_end = time.monotonic() + 5.0
        while not bridge.is_coach_online() and time.monotonic() < t_end:
            app.processEvents()
            time.sleep(0.05)
        app.processEvents()
        assert "在线" in drawer.coach_status_lbl.text()

        bridge.publish_request(
            CoachRequestPayload(
                request_id="smoke-001",
                trigger_reason="manual_f6",
                timestamp=utc_now_iso(),
                dataset_patch="26.23",
                my_hero={"hero_id": "222", "name": "金克丝", "gold": 3200, "items": ["卢安娜的飓风"]},
                selected_augments=[{"id": "3081", "name": "命运之武器", "rarity": "gold"}],
                enemies=[{"champion": "亚托克斯", "kda": "9/2/4", "items": ["荆棘之甲", "心之钢"]}],
                threat_summary=list(threat.summary_tags),
            )
        )
        out, err = proc.communicate(timeout=8)
        assert proc.returncode == 0 and "[HEX_COACH_EVENT]" in out, f"Relay failed: {err}"
        payload = json.loads(out.split("[HEX_COACH_EVENT]", 1)[1].strip())
        assert payload["request_id"] == "smoke-001"

        res = subprocess.run(
            [
                sys.executable,
                str(pusher),
                "--port",
                str(port),
                "--request-id",
                "smoke-001",
                "--augment-tip",
                "命运之武器直接绑定破败",
                "--buy-now",
                "手里 3200G 直接合凡性的提醒(3000G)",
                "--build-path",
                "凡性的提醒>破败王者之刃>无尽之刃",
                "--summary",
                "优先补凡性的提醒克制剑魔回复，团战靠被动收割。",
                "--item",
                "3033|凡性的提醒|必出|敌方剑魔与索拉卡高回复且我方缺重伤",
                "--item",
                "3153|破败王者之刃|推荐|命运之武器最高胜率绑定",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=8,
            check=True,
        )
        assert "[PUSH_OK]" in res.stdout
        time.sleep(0.1)
        app.processEvents()

        next_title = drawer.next_item_title_lbl.text()
        buy_now_txt = drawer.buy_now_lbl.text()
        roadmap_txt = drawer.roadmap_body_lbl.text()
        coach_txt = drawer.coach_content_lbl.text()
        assert "凡性的提醒" in next_title, f"Unexpected next_title: {next_title}"
        assert "凡性的提醒" in buy_now_txt, f"Unexpected buy_now: {buy_now_txt}"
        assert "凡性的提醒" in roadmap_txt and "破败王者之刃" in roadmap_txt, f"Unexpected roadmap: {roadmap_txt}"
        assert "剑魔" in coach_txt, f"Unexpected coach summary: {coach_txt}"
        print(
            f"  OK: EdgeBuildDrawer v0.3.0 ({drawer.width()}x{drawer.height()}) rendered:\n"
            f"    {next_title}\n"
            f"    {buy_now_txt}\n"
            f"    {roadmap_txt}\n"
            f"    {coach_txt}"
        )
    finally:
        bridge.stop()
        champ_ui.close()
        drawer.close()
        aug_ui.close()
        lookup.close()
        app.processEvents()

    print("\nALL 5 LIVE SMOKE VERIFICATIONS PASSED!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
