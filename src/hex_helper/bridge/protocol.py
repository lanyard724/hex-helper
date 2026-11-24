"""JSON protocol schemas shared between HexHelper and Antigravity coach relay."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class CoachRequestPayload:
    request_id: str
    trigger_reason: str
    timestamp: str
    dataset_patch: str
    my_hero: dict[str, Any]
    selected_augments: list[dict[str, Any]] = field(default_factory=list)
    offered_augments: list[dict[str, Any]] = field(default_factory=list)
    allies: list[dict[str, Any]] = field(default_factory=list)
    enemies: list[dict[str, Any]] = field(default_factory=list)
    threat_summary: list[str] = field(default_factory=list)
    local_engine_top_items: list[dict[str, Any]] = field(default_factory=list)
    committed_build_plan: dict[str, Any] = field(default_factory=dict)
    special_augment_builds: list[dict[str, Any]] = field(default_factory=list)
    hero_signature_hint: dict[str, Any] = field(default_factory=dict)
    augment_combo_context: dict[str, Any] = field(default_factory=dict)
    lobby_scout: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CoachRequestPayload:
        return cls(
            request_id=str(raw.get("request_id", "")),
            trigger_reason=str(raw.get("trigger_reason", "manual")),
            timestamp=str(raw.get("timestamp", utc_now_iso())),
            dataset_patch=str(raw.get("dataset_patch", "")),
            my_hero=dict(raw.get("my_hero") or {}),
            selected_augments=list(raw.get("selected_augments") or []),
            offered_augments=list(raw.get("offered_augments") or []),
            allies=list(raw.get("allies") or []),
            enemies=list(raw.get("enemies") or []),
            threat_summary=list(raw.get("threat_summary") or []),
            local_engine_top_items=list(raw.get("local_engine_top_items") or []),
            committed_build_plan=dict(raw.get("committed_build_plan") or {}),
            special_augment_builds=list(raw.get("special_augment_builds") or []),
            hero_signature_hint=dict(raw.get("hero_signature_hint") or {}),
            augment_combo_context=dict(raw.get("augment_combo_context") or {}),
            lobby_scout=dict(raw.get("lobby_scout") or {}),
        )


OPENING_BRIEF_KEYS: tuple[str, ...] = ("enemy_threat", "augment_goal", "opening_buy")


def normalize_opening_brief(raw: Any) -> dict[str, str]:
    """Normalize optional opening_brief dict to stripped non-empty string values."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key in OPENING_BRIEF_KEYS:
        val = raw.get(key)
        if val is None:
            continue
        cleaned = str(val).strip()
        if cleaned:
            out[key] = cleaned
    return out


@dataclass
class CoachAdviceItem:
    item_id: str
    name: str
    priority: str = "推荐"  # "必出" | "推荐" | "可选"
    reason: str = ""


@dataclass
class CoachAdvicePayload:
    request_id: str
    next_items: list[CoachAdviceItem] = field(default_factory=list)
    buy_now: str = ""
    build_path: list[str] = field(default_factory=list)
    augment_pick_advice: str = ""
    tactical_summary: str = ""
    updated_at: str = field(default_factory=utc_now_iso)
    source: str = "Antigravity"
    opening_brief: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["opening_brief"] = normalize_opening_brief(self.opening_brief)
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CoachAdvicePayload:
        items: list[CoachAdviceItem] = []
        for it in raw.get("next_items") or []:
            if isinstance(it, dict):
                items.append(
                    CoachAdviceItem(
                        item_id=str(it.get("item_id", "")),
                        name=str(it.get("name", "")),
                        priority=str(it.get("priority", "推荐")),
                        reason=str(it.get("reason", "")),
                    )
                )
        raw_path = raw.get("build_path") or []
        if isinstance(raw_path, str):
            build_path = [p.strip() for p in raw_path.replace("->", ">").split(">") if p.strip()]
        else:
            build_path = [str(p).strip() for p in raw_path if str(p).strip()]
        return cls(
            request_id=str(raw.get("request_id", "")),
            next_items=items,
            buy_now=str(raw.get("buy_now", "")),
            build_path=build_path,
            augment_pick_advice=str(raw.get("augment_pick_advice", "")),
            tactical_summary=str(raw.get("tactical_summary", "")),
            updated_at=str(raw.get("updated_at") or utc_now_iso()),
            source=str(raw.get("source") or "Antigravity"),
            opening_brief=normalize_opening_brief(raw.get("opening_brief")),
        )
