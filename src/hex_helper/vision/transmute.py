"""Secondary OCR resolver for Transmute / Random-Grant augments (质变：黄金阶 / 质变：棱彩阶 / 质变：混沌 / 潘朵拉的盒子).

Uses 3 read-only visual channels (100% ACE-safe, zero memory hooks):
1. Bottom-left in-game chat system notification (`获得了 ...` / `质变 ...`)
2. Center / upper-center screen reveal toast / card popup right after picker closes
3. Live cursor hover-tooltip auto-capture (when the player hovers over the newly rolled augment icon on HUD or Tab)
"""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from dataclasses import dataclass, field

import cv2
import numpy as np

from hex_helper.data.provider import AugmentInfo
from hex_helper.vision.capture import WindowRect
from hex_helper.vision.detector import filter_excluded_lines
from hex_helper.vision.matcher import AugmentMatcher, normalize_ocr_text
from hex_helper.vision.ocr_winrt import OcrLine, WinRtOcrEngine


@dataclass(frozen=True)
class TransmuteSpec:
    augment_id: str
    name: str
    expected_count: int  # 0 = dynamic (replaces_all -> prior augment count)
    target_rarity: str | None  # "silver" | "gold" | "prismatic" | None (any)
    replaces_all: bool = False


TRANSMUTE_SPECS: dict[str, TransmuteSpec] = {
    "1237": TransmuteSpec("1237", "质变：黄金阶", expected_count=1, target_rarity="gold", replaces_all=False),
    "1238": TransmuteSpec("1238", "质变：棱彩阶", expected_count=1, target_rarity="prismatic", replaces_all=False),
    "1243": TransmuteSpec("1243", "质变：混沌", expected_count=2, target_rarity=None, replaces_all=False),
    "12317": TransmuteSpec("12317", "潘朵拉的盒子", expected_count=0, target_rarity=None, replaces_all=True),
}

# Augments that can never be the final rolled target of a transmute secondary scan
NON_TARGET_AUGMENT_IDS: frozenset[str] = frozenset(
    {
        "1237",   # 质变：黄金阶
        "1238",   # 质变：棱彩阶
        "1243",   # 质变：混沌
        "12317",  # 潘朵拉的盒子
        "1402",   # 属性叠属性叠属性！
        "1403",   # 属性叠属性！
        "1404",   # 属性！
    }
)

_CHAT_STRONG_CUES: tuple[str, ...] = ("获得", "质变", "变成", "转化", "强化符文", "随机获得")
_BRACKET_RE = re.compile(r"[【\[「《〖]([^】\]」》〗]{2,20})[】\]」》〗]")
_AFTER_CUE_RE = re.compile(r"(?:获得了?|质变为|质变|变成了?|转化为|强化符文[:：]?)\s*[:：\-—>]*\s*(.+)")
_NON_AUGMENT_TOOLTIP_CUES: tuple[str, ...] = (
    "点击以购买",
    "出售价格",
    "法力消耗",
    "生命值消耗",
    "能量消耗",
    "合成路线",
    "唯一被动",
)
_RARITY_LABELS: tuple[str, ...] = ("棱彩阶", "黄金阶", "白银阶", "棱彩", "黄金", "白银", "强化符文")
_WS_RE = re.compile(r"\s+")


def _compact_ws(text: str) -> str:
    """Windows WinRT zh-Hans-CN OCR inserts spaces between CJK glyphs; collapse them before cue matching."""
    return _WS_RE.sub("", str(text or ""))


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def get_cursor_pos() -> tuple[int, int] | None:
    try:
        pt = _POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return int(pt.x), int(pt.y)
    except Exception:
        pass
    return None


def is_transmute_augment(augment_id: str) -> bool:
    return str(augment_id) in TRANSMUTE_SPECS


def get_transmute_spec(augment_id: str) -> TransmuteSpec | None:
    return TRANSMUTE_SPECS.get(str(augment_id))


@dataclass
class PendingTransmute:
    source_augment_id: str
    source_name: str
    expected_count: int
    target_rarity: str | None
    started_mono: float
    replaces_all: bool = False
    resolved_ids: list[str] = field(default_factory=list)
    excluded_fading_ids: tuple[str, ...] = ()
    placeholder_replaced: bool = False
    partial_coach_fired: bool = False

    @property
    def remaining_count(self) -> int:
        return max(0, self.expected_count - len(self.resolved_ids))


@dataclass(frozen=True)
class ResolvedTransmuteHit:
    augment: AugmentInfo
    confidence: float
    channel: str  # "chat" | "reveal_banner" | "hover_tooltip"
    raw_text: str


class TransmuteRevealWatcher:
    """Multi-channel WinRT OCR watcher that resolves what augment(s) rolled from a Transmute pick."""

    ACTIVE_REVEAL_WINDOW_S = 12.0
    FADING_PICKER_GRACE_S = 1.8
    HOVER_POLL_INTERVAL_S = 0.55

    def __init__(self, ocr_engine: WinRtOcrEngine, augments: list[AugmentInfo]) -> None:
        self.ocr = ocr_engine
        self._all_augments: list[AugmentInfo] = []
        self._votes: dict[str, list[float]] = {}
        self._last_hover_thumb: np.ndarray | None = None
        self._last_hover_cursor: tuple[int, int] | None = None
        self._last_hover_scan_mono: float = 0.0
        self._passive_last_cursor: tuple[int, int] | None = None
        self._passive_steady_since: float = 0.0
        self._passive_last_scan_mono: float = 0.0
        self._passive_last_thumb: np.ndarray | None = None
        self.update_catalog(augments)

    def update_catalog(self, augments: list[AugmentInfo]) -> None:
        self._all_augments = list(augments)

    def reset(self) -> None:
        self._votes.clear()
        self._last_hover_thumb = None
        self._last_hover_cursor = None
        self._last_hover_scan_mono = 0.0
        self._passive_last_cursor = None
        self._passive_steady_since = 0.0
        self._passive_last_scan_mono = 0.0
        self._passive_last_thumb = None

    def _allowed_candidates(
        self,
        pending: PendingTransmute,
        already_owned_ids: set[str],
        now_mono: float,
    ) -> list[AugmentInfo]:
        elapsed = now_mono - pending.started_mono
        blocked = set(NON_TARGET_AUGMENT_IDS) | set(pending.resolved_ids)
        if not pending.replaces_all:
            blocked |= set(already_owned_ids)
        if elapsed <= self.FADING_PICKER_GRACE_S:
            blocked |= set(pending.excluded_fading_ids)

        out: list[AugmentInfo] = []
        for aug in self._all_augments:
            if aug.augment_id in blocked:
                continue
            if pending.target_rarity and aug.rarity != pending.target_rarity:
                continue
            out.append(aug)
        return out

    def _record_vote(self, aug_id: str, now_mono: float, needed: int = 2, window_s: float = 4.0) -> bool:
        hist = [ts for ts in self._votes.get(aug_id, []) if (now_mono - ts) <= window_s]
        hist.append(now_mono)
        self._votes[aug_id] = hist
        return len(hist) >= needed

    @staticmethod
    def _infer_rarity_hint(lines: list[OcrLine]) -> str | None:
        joined = "".join(_compact_ws(l.text) for l in lines)
        if "棱彩" in joined:
            return "prismatic"
        if "黄金" in joined:
            return "gold"
        if "白银" in joined:
            return "silver"
        return None

    @staticmethod
    def _strict_match_title(
        matcher: AugmentMatcher,
        text: str,
        *,
        rarity_hint: str | None = None,
        allow_fuzzy_3plus: bool = True,
        has_strong_context: bool = False,
    ) -> tuple[AugmentInfo, float] | None:
        norm = normalize_ocr_text(text)
        if len(norm) < 2:
            return None
        query_min = 0.67 if (allow_fuzzy_3plus and has_strong_context) else 0.86
        res = matcher.match_line(text, rarity_hint=rarity_hint, min_score=query_min)
        if res is None:
            return None
        cand_norm = normalize_ocr_text(res.augment.name)
        if len(cand_norm) <= 2:
            # Short 2-char names (e.g. 残忍/冰寒/大腕/力量) require exact match to prevent false positives
            if norm == cand_norm:
                return res.augment, 1.0
            return None
        if norm == cand_norm:
            return res.augment, 1.0
        if allow_fuzzy_3plus and len(norm) <= len(cand_norm) + 4 and res.score >= 0.90:
            return res.augment, res.score
        if (
            allow_fuzzy_3plus
            and has_strong_context
            and len(cand_norm) >= 4
            and abs(len(norm) - len(cand_norm)) <= 2
            and res.score >= 0.68
        ):
            # Tolerates a 1-glyph WinRT OCR misread on a 4+ char title (e.g. 炼狱导冒 -> 炼狱导管 = 0.6975)
            # ONLY when backed by strong structural context (tooltip desc/rarity below or chat/banner grant cue).
            return res.augment, res.score
        return None

    @staticmethod
    def _has_supporting_desc_below(title_line: OcrLine, all_lines: list[OcrLine]) -> bool:
        """Check whether a multi-character description or rarity line sits directly below a title line."""
        for other in all_lines:
            if other is title_line:
                continue
            dy = other.y - title_line.y
            if 10.0 <= dy <= 190.0 and abs(other.cx - title_line.cx) <= 240.0:
                compact_o = _compact_ws(other.text)
                norm_o = normalize_ocr_text(compact_o)
                if len(norm_o) >= 6 or any(rl in compact_o for rl in _RARITY_LABELS):
                    return True
        return False

    def _scan_chat_lines(
        self,
        lines: list[OcrLine],
        candidates: list[AugmentInfo],
        matcher: AugmentMatcher,
        pending: PendingTransmute,
        now_mono: float,
    ) -> list[ResolvedTransmuteHit]:
        hits: list[ResolvedTransmuteHit] = []
        found_ids: set[str] = set()
        # Sort longest candidate names first so substring extraction prefers exact full names (e.g. 珠光护手 vs 护手)
        cand_by_len = sorted(
            ((aug, normalize_ocr_text(aug.name)) for aug in candidates),
            key=lambda x: len(x[1]),
            reverse=True,
        )

        for line in lines:
            raw = _compact_ws(line.text)
            if len(raw) < 2:
                continue
            has_strong_cue = any(c in raw for c in _CHAT_STRONG_CUES)
            bracket_tokens = _BRACKET_RE.findall(raw)

            # 1) Explicit bracketed tokens (e.g. "获得了 【死亡之环】" or "[珠光护手] & [炼狱导管]")
            for tok in bracket_tokens:
                m = self._strict_match_title(
                    matcher,
                    tok,
                    rarity_hint=pending.target_rarity,
                    has_strong_context=has_strong_cue,
                )
                if m is not None and m[0].augment_id not in found_ids:
                    needed = 1 if has_strong_cue else 2
                    if self._record_vote(m[0].augment_id, now_mono, needed=needed):
                        found_ids.add(m[0].augment_id)
                        hits.append(ResolvedTransmuteHit(m[0], m[1], "chat", raw))

            # 2) Strong chat notification sentence without brackets (e.g. "你获得了 死亡之环", "质变：混沌 获得 珠光护手 炼狱导管")
            if has_strong_cue:
                tail_m = _AFTER_CUE_RE.search(raw)
                search_segment = normalize_ocr_text(tail_m.group(1) if tail_m else raw)
                # Strip source transmute name so we don't match fragments of it
                src_norm = normalize_ocr_text(pending.source_name)
                if src_norm:
                    search_segment = search_segment.replace(src_norm, "")
                # Direct substring check for exact candidate names inside the grant message
                remaining_seg = search_segment
                for aug, c_norm in cand_by_len:
                    if not c_norm or aug.augment_id in found_ids:
                        continue
                    if c_norm in remaining_seg:
                        # For 2-char names, require whole-segment match or delimiter/bracket so "力量" doesn't hit random text
                        if len(c_norm) <= 2 and remaining_seg != c_norm:
                            continue
                        found_ids.add(aug.augment_id)
                        hits.append(ResolvedTransmuteHit(aug, 0.98, "chat", raw))
                        remaining_seg = remaining_seg.replace(c_norm, "", 1)
                        if len(hits) >= pending.remaining_count:
                            return hits
                # Fallback fuzzy match if whole tail segment is a slightly OCR-noisy 3+ char augment name
                if remaining_seg and len(remaining_seg) >= 3:
                    m = self._strict_match_title(
                        matcher,
                        remaining_seg,
                        rarity_hint=pending.target_rarity,
                        has_strong_context=True,
                    )
                    if m is not None and m[0].augment_id not in found_ids:
                        found_ids.add(m[0].augment_id)
                        hits.append(ResolvedTransmuteHit(m[0], m[1], "chat", raw))
        return hits

    def _scan_reveal_or_tooltip_lines(
        self,
        lines: list[OcrLine],
        matcher: AugmentMatcher,
        pending: PendingTransmute,
        channel: str,
        now_mono: float,
    ) -> list[ResolvedTransmuteHit]:
        if not lines:
            return []
        joined_all = "".join(_compact_ws(l.text) for l in lines)
        if channel == "hover_tooltip" and any(bad in joined_all for bad in _NON_AUGMENT_TOOLTIP_CUES):
            return []

        has_banner_cue = any(c in joined_all for c in _CHAT_STRONG_CUES)
        inferred_rarity = pending.target_rarity or self._infer_rarity_hint(lines)
        hits: list[ResolvedTransmuteHit] = []
        found_ids: set[str] = set()

        for line in lines:
            has_desc = self._has_supporting_desc_below(line, lines)
            has_strong_context = has_desc or (channel != "hover_tooltip" and has_banner_cue)
            m = self._strict_match_title(
                matcher,
                line.text,
                rarity_hint=inferred_rarity,
                has_strong_context=has_strong_context,
            )
            if m is None:
                continue
            aug, conf = m
            if aug.augment_id in found_ids:
                continue

            if channel == "hover_tooltip":
                # Hover tooltips ALWAYS carry description / rarity lines beneath the augment title
                needed = 1 if (has_desc and len(normalize_ocr_text(aug.name)) >= 3) else 2
            else:
                needed = 1 if (has_banner_cue or (has_desc and len(normalize_ocr_text(aug.name)) >= 3)) else 2

            if self._record_vote(aug.augment_id, now_mono, needed=needed):
                found_ids.add(aug.augment_id)
                hits.append(ResolvedTransmuteHit(aug, conf, channel, line.text))
                if len(hits) >= pending.remaining_count:
                    break
        return hits

    def _scan_crop(
        self,
        frame_bgr: np.ndarray,
        crop_box: tuple[int, int, int, int],
        game_rect: WindowRect,
        exclude_rects: list[tuple[int, int, int, int]],
    ) -> list[OcrLine]:
        x0, y0, x1, y1 = crop_box
        crop = frame_bgr[y0:y1, x0:x1]
        if crop.size == 0 or crop.shape[0] < 24 or crop.shape[1] < 40:
            return []
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        if float(np.std(gray)) < 10.0:
            return []
        raw_lines = self.ocr.recognize_lines(crop, dual_pass=True)
        return filter_excluded_lines(raw_lines, game_rect.left + x0, game_rect.top + y0, exclude_rects)

    def process_frame(
        self,
        frame_bgr: np.ndarray,
        game_rect: WindowRect,
        pending: PendingTransmute,
        already_owned_ids: set[str],
        exclude_rects: list[tuple[int, int, int, int]] | None = None,
        cursor_pos: tuple[int, int] | None = None,
        now_mono: float = 0.0,
    ) -> list[ResolvedTransmuteHit]:
        """Inspect active reveal zones + cursor hover tooltip for newly rolled transmute augment(s)."""
        if pending.remaining_count <= 0 or frame_bgr is None or frame_bgr.size == 0:
            return []

        candidates = self._allowed_candidates(pending, already_owned_ids, now_mono)
        if not candidates:
            return []

        matcher = AugmentMatcher(candidates)
        excl = exclude_rects or []
        fh, fw = frame_bgr.shape[:2]
        elapsed = now_mono - pending.started_mono
        collected: list[ResolvedTransmuteHit] = []
        seen_ids: set[str] = set()

        def _merge(new_hits: list[ResolvedTransmuteHit]) -> None:
            for h in new_hits:
                if h.augment.augment_id not in seen_ids:
                    seen_ids.add(h.augment.augment_id)
                    collected.append(h)

        # Channel 1 & 2: Active post-pick reveal window (first 12s after selecting a Transmute augment)
        if elapsed <= self.ACTIVE_REVEAL_WINDOW_S:
            # 1) Bottom-left chat box (system grant announcement)
            chat_box = (0, int(fh * 0.48), int(fw * 0.44), int(fh * 0.88))
            chat_lines = self._scan_crop(frame_bgr, chat_box, game_rect, excl)
            _merge(self._scan_chat_lines(chat_lines, candidates, matcher, pending, now_mono))
            if len(collected) >= pending.remaining_count:
                return collected[: pending.remaining_count]

            # 2) Center / upper-center reveal toast / single-or-double card popup
            reveal_box = (int(fw * 0.16), int(fh * 0.12), int(fw * 0.84), int(fh * 0.72))
            reveal_lines = self._scan_crop(frame_bgr, reveal_box, game_rect, excl)
            _merge(self._scan_reveal_or_tooltip_lines(reveal_lines, matcher, pending, "reveal_banner", now_mono))
            if len(collected) >= pending.remaining_count:
                return collected[: pending.remaining_count]

        # Channel 3: Live cursor hover-tooltip auto-capture (always available while transmute is pending!)
        if cursor_pos is not None:
            cx_scr, cy_scr = cursor_pos
            in_game = (
                game_rect.left <= cx_scr <= game_rect.right
                and game_rect.top <= cy_scr <= game_rect.bottom
            )
            in_excl = any(l <= cx_scr <= r and t <= cy_scr <= b for (l, t, r, b) in excl)
            can_scan_hover = (
                elapsed <= self.ACTIVE_REVEAL_WINDOW_S
                or (now_mono - self._last_hover_scan_mono) >= self.HOVER_POLL_INTERVAL_S
            )
            if in_game and not in_excl and can_scan_hover:
                self._last_hover_scan_mono = now_mono
                rx = cx_scr - game_rect.left
                ry = cy_scr - game_rect.top
                # LoL tooltips pop above/beside the hovered augment icon on HUD or Tab scoreboard
                half_w = max(240, int(fw * 0.16))
                above_h = max(280, int(fh * 0.28))
                below_h = max(120, int(fh * 0.10))
                hx0 = max(0, rx - half_w)
                hx1 = min(fw, rx + half_w)
                hy0 = max(0, ry - above_h)
                hy1 = min(fh, ry + below_h)
                hover_crop = frame_bgr[hy0:hy1, hx0:hx1]
                if hover_crop.size > 0 and hover_crop.shape[0] >= 40 and hover_crop.shape[1] >= 80:
                    gray = cv2.cvtColor(hover_crop, cv2.COLOR_BGR2GRAY)
                    thumb = cv2.resize(gray, (96, 64), interpolation=cv2.INTER_AREA)
                    cursor_moved = (
                        self._last_hover_cursor is None
                        or abs(cx_scr - self._last_hover_cursor[0]) >= 16
                        or abs(cy_scr - self._last_hover_cursor[1]) >= 16
                    )
                    # Skip unchanged static frame if cursor did not move and no votes pending
                    changed = (
                        cursor_moved
                        or self._last_hover_thumb is None
                        or self._last_hover_thumb.shape != thumb.shape
                        or float(np.mean(np.abs(thumb.astype(np.int16) - self._last_hover_thumb.astype(np.int16)))) >= 0.8
                        or bool(self._votes)
                    )
                    self._last_hover_thumb = thumb
                    self._last_hover_cursor = (cx_scr, cy_scr)
                    if changed and float(np.std(gray)) >= 10.0:
                        hover_lines = self._scan_crop(frame_bgr, (hx0, hy0, hx1, hy1), game_rect, excl)
                        _merge(self._scan_reveal_or_tooltip_lines(hover_lines, matcher, pending, "hover_tooltip", now_mono))

        return collected[: pending.remaining_count]

    def scan_passive_hud_hover(
        self,
        frame_bgr: np.ndarray,
        game_rect: WindowRect,
        already_owned_ids: set[str],
        exclude_rects: list[tuple[int, int, int, int]] | None = None,
        cursor_pos: tuple[int, int] | None = None,
        now_mono: float = 0.0,
    ) -> ResolvedTransmuteHit | None:
        """Low-cost passive HUD/Tab hover sync that auto-fills any missing owned augment without manual clicking.

        Runs OCR ONLY when the cursor pauses on the bottom HUD or Tab scoreboard tooltip zone,
        and strictly requires BOTH a valid augment title AND an explicit augment rarity/type cue
        (白银/黄金/棱彩/强化符文) so champion skills and shop items never false-trigger.
        """
        if cursor_pos is None or frame_bgr is None or frame_bgr.size == 0 or not self._all_augments:
            return None
        cx_scr, cy_scr = cursor_pos
        excl = exclude_rects or []
        if not (
            game_rect.left <= cx_scr <= game_rect.right
            and game_rect.top <= cy_scr <= game_rect.bottom
        ):
            return None
        if any(l <= cx_scr <= r and t <= cy_scr <= b for (l, t, r, b) in excl):
            return None

        fh, fw = frame_bgr.shape[:2]
        rx = cx_scr - game_rect.left
        ry = cy_scr - game_rect.top
        x_rel = rx / max(1, fw)
        y_rel = ry / max(1, fh)

        in_bottom_hud = (0.14 <= x_rel <= 0.86) and (y_rel >= 0.72)
        in_tab_zone = (0.12 <= x_rel <= 0.88) and (0.16 <= y_rel < 0.72)
        if not (in_bottom_hud or in_tab_zone):
            self._passive_last_cursor = cursor_pos
            self._passive_steady_since = now_mono
            return None

        # Track cursor steadiness so fast mouse kiting in combat NEVER invokes OCR (~0% CPU in fights)
        if (
            self._passive_last_cursor is None
            or abs(cx_scr - self._passive_last_cursor[0]) > 22
            or abs(cy_scr - self._passive_last_cursor[1]) > 22
        ):
            self._passive_last_cursor = cursor_pos
            self._passive_steady_since = now_mono
            return None

        min_dwell = 0.18 if in_bottom_hud else 0.32
        if (now_mono - self._passive_steady_since) < min_dwell:
            return None
        if (now_mono - self._passive_last_scan_mono) < 0.70:
            return None

        half_w = max(240, int(fw * 0.16))
        above_h = max(280, int(fh * 0.28))
        below_h = max(120, int(fh * 0.10))
        hx0 = max(0, rx - half_w)
        hx1 = min(fw, rx + half_w)
        hy0 = max(0, ry - above_h)
        hy1 = min(fh, ry + below_h)
        hover_crop = frame_bgr[hy0:hy1, hx0:hx1]
        if hover_crop.size == 0 or hover_crop.shape[0] < 40 or hover_crop.shape[1] < 80:
            return None

        gray = cv2.cvtColor(hover_crop, cv2.COLOR_BGR2GRAY)
        if float(np.std(gray)) < 10.0:
            return None
        thumb = cv2.resize(gray, (96, 64), interpolation=cv2.INTER_AREA)
        if (
            self._passive_last_thumb is not None
            and self._passive_last_thumb.shape == thumb.shape
            and float(np.mean(np.abs(thumb.astype(np.int16) - self._passive_last_thumb.astype(np.int16)))) < 0.8
        ):
            return None
        self._passive_last_thumb = thumb
        self._passive_last_scan_mono = now_mono

        lines = self._scan_crop(frame_bgr, (hx0, hy0, hx1, hy1), game_rect, excl)
        if not lines:
            return None
        joined_all = "".join(_compact_ws(l.text) for l in lines)
        if any(bad in joined_all for bad in _NON_AUGMENT_TOOLTIP_CUES):
            return None

        inferred_rarity = self._infer_rarity_hint(lines)
        if inferred_rarity is None and "强化符文" not in joined_all:
            return None

        blocked = set(NON_TARGET_AUGMENT_IDS) | set(already_owned_ids)
        candidates = [a for a in self._all_augments if a.augment_id not in blocked]
        if not candidates:
            return None
        matcher = AugmentMatcher(candidates)
        for line in lines:
            has_desc = self._has_supporting_desc_below(line, lines)
            if not has_desc:
                continue
            m = self._strict_match_title(
                matcher,
                line.text,
                rarity_hint=inferred_rarity,
                has_strong_context=True,
            )
            if m is not None:
                return ResolvedTransmuteHit(m[0], m[1], "hud_hover_sync", line.text)
        return None
