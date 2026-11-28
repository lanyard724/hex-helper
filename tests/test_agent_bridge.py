import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from hex_helper.bridge.agent_bridge import AgentBridgeServer
from hex_helper.bridge.protocol import CoachAdvicePayload, CoachRequestPayload


def test_bridge_relay_and_push_roundtrip(tmp_path: Path):
    received_advice: list[CoachAdvicePayload] = []
    online_events: list[bool] = []
    advice_event = threading.Event()

    def on_adv(adv: CoachAdvicePayload) -> None:
        received_advice.append(adv)
        advice_event.set()

    bridge = AgentBridgeServer(
        bridge_dir=tmp_path / "bridge",
        port=0,  # OS assigns free ephemeral port
        on_advice=on_adv,
        on_online_changed=lambda ok: online_events.append(ok),
    )
    port = bridge.start()
    try:
        assert bridge.is_coach_online() is False

        project_root = Path(__file__).resolve().parents[1]
        relay_script = project_root / "scripts" / "coach_relay.py"
        push_script = project_root / "scripts" / "push_advice.py"
        cursor_file = tmp_path / "cursor.txt"

        # Launch real coach_relay.py waiting for an event
        proc = subprocess.Popen(
            [
                sys.executable,
                str(relay_script),
                "--wait",
                "--port",
                str(port),
                "--cursor-file",
                str(cursor_file),
                "--timeout",
                "12",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        # Wait until relay sends heartbeat and marks coach online
        deadline = time.monotonic() + 5.0
        while not bridge.is_coach_online() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert bridge.is_coach_online() is True
        assert True in online_events

        # Publish a real match snapshot from HexHelper
        req = CoachRequestPayload(
            request_id="req-001",
            trigger_reason="manual_f6",
            timestamp="2026-11-21T16:10:00+08:00",
            dataset_patch="26.23",
            my_hero={"hero_id": "222", "name": "金克丝", "gold": 3100, "items": ["3085"]},
            selected_augments=[{"id": "3081", "name": "命运之武器", "rarity": "gold"}],
            enemies=[{"champion": "亚托克斯", "kda": "9/2/4", "items": ["3075", "3084"]}],
            threat_summary=["敌方高回复(缺重伤)"],
        )
        bridge.publish_request(req)
        assert (tmp_path / "bridge" / "request.json").exists()

        # Relay should wake up immediately and exit 0 with [HEX_COACH_EVENT] + JSON
        stdout, stderr = proc.communicate(timeout=8)
        assert proc.returncode == 0, f"relay failed: {stderr}"
        assert "[HEX_COACH_EVENT]" in stdout
        json_line = stdout.split("[HEX_COACH_EVENT]", 1)[1].strip()
        parsed_req = json.loads(json_line)
        assert parsed_req["request_id"] == "req-001"
        assert parsed_req["my_hero"]["name"] == "金克丝"

        # Push Antigravity advice back via real push_advice.py CLI
        import os

        env = os.environ.copy()
        env["HEX_HELPER_DATA_DIR"] = str(tmp_path)
        push_res = subprocess.run(
            [
                sys.executable,
                str(push_script),
                "--port",
                str(port),
                "--request-id",
                "req-001",
                "--augment-tip",
                "命运之武器直接绑定破败",
                "--buy-now",
                "手里 3100G 直接合凡性的提醒(3000G)",
                "--build-path",
                "凡性的提醒>破败王者之刃>无尽之刃",
                "--summary",
                "先补凡性的提醒克制剑魔回复，团战站后排等启动。",
                "--item",
                "3033|凡性的提醒|必出|敌方剑魔9/2高回复且我方缺重伤",
                "--item",
                "3153|破败王者之刃|推荐|命运之武器最高胜率绑定",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=8,
            check=True,
        )
        assert "[PUSH_OK]" in push_res.stdout
        assert advice_event.wait(timeout=3.0)
        assert len(received_advice) == 1
        adv = received_advice[0]
        assert adv.request_id == "req-001"
        assert adv.next_items[0].item_id == "3033"
        assert adv.next_items[0].priority == "必出"
        assert "凡性的提醒" in adv.buy_now
        assert adv.build_path == ["凡性的提醒", "破败王者之刃", "无尽之刃"]
        assert (tmp_path / "bridge" / "advice.json").exists()
    finally:
        bridge.stop()
