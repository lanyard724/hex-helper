"""237-augment constrained Chinese fuzzy dictionary matcher with rarity boost."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from hex_helper.data.provider import AugmentInfo

_STRIP_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")
NEAR_MISS_MIN_SCORE = 0.42


def normalize_ocr_text(text: str) -> str:
    return _STRIP_RE.sub("", str(text or "")).lower()


@dataclass(frozen=True)
class MatchResult:
    augment: AugmentInfo
    score: float
    raw_text: str
    top2_augment_id: str = ""
    top2_augment_name: str = ""
    top2_score: float = 0.0
    margin: float = 0.0
    seq_ratio: float = 0.0
    jaccard_ratio: float = 0.0
    substring_hit: bool = False
    rarity_boosted: bool = False
    near_miss_top1_id: str = ""
    near_miss_top1_score: float = 0.0


class AugmentMatcher:
    """Fuzzy matcher strictly bounded to the active ARAM Mayhem augment catalog."""

    def __init__(self, augments: list[AugmentInfo]) -> None:
        self._entries: list[tuple[AugmentInfo, str, set[str]]] = []
        self.update_catalog(augments)

    def update_catalog(self, augments: list[AugmentInfo]) -> None:
        entries: list[tuple[AugmentInfo, str, set[str]]] = []
        for aug in augments:
            norm = normalize_ocr_text(aug.name)
            if norm:
                entries.append((aug, norm, set(norm)))
        self._entries = entries

    def _scan_candidates(
        self,
        norm: str,
        rarity_hint: str | None = None,
    ) -> tuple[
        AugmentInfo | None,
        float,
        AugmentInfo | None,
        float,
        float,
        float,
        bool,
        bool,
    ]:
        best_aug: AugmentInfo | None = None
        best_score = 0.0
        second_aug: AugmentInfo | None = None
        second_score = 0.0
        best_seq = 0.0
        best_jaccard = 0.0
        best_sub_hit = False
        best_rarity_boost = False
        norm_chars = set(norm)

        for aug, cand_norm, cand_chars in self._entries:
            rarity_boost = bool(rarity_hint and aug.rarity == rarity_hint)
            if norm == cand_norm:
                seq = 1.0
                jaccard = 1.0
                sub_hit = False
                score = 1.0 + (0.05 if rarity_boost else 0.0)
            else:
                # Substring boost strictly restricted to 3+ char names within +4 chars of candidate length
                # so 2-char names (e.g. 3171 力量 / 1404 属性！) NEVER false-match 力量碎片 / 属性锻造器 / long tooltips
                allow_sub = len(cand_norm) >= 3 and cand_norm in norm and len(norm) <= len(cand_norm) + 4
                # Reject wild length mismatch unless one clearly contains the other
                len_ratio = min(len(norm), len(cand_norm)) / max(len(norm), len(cand_norm))
                if len_ratio < 0.45 and not allow_sub:
                    continue
                if len(cand_norm) <= 2 and len(norm) >= 4:
                    continue

                seq = SequenceMatcher(None, norm, cand_norm).ratio()
                union = norm_chars | cand_chars
                jaccard = (len(norm_chars & cand_chars) / len(union)) if union else 0.0
                score = 0.65 * seq + 0.35 * jaccard
                sub_hit = False

                if allow_sub:
                    score = max(score, 0.82 + 0.1 * (len(cand_norm) / len(norm)))
                    sub_hit = True
                if rarity_boost:
                    score += 0.12

            if score > best_score:
                second_score = best_score
                second_aug = best_aug
                best_score = score
                best_aug = aug
                best_seq = seq
                best_jaccard = jaccard
                best_sub_hit = sub_hit
                best_rarity_boost = rarity_boost
            elif score > second_score:
                second_score = score
                second_aug = aug

        return (
            best_aug,
            best_score,
            second_aug,
            second_score,
            best_seq,
            best_jaccard,
            best_sub_hit,
            best_rarity_boost,
        )

    def match_line_with_diagnostics(
        self,
        raw_text: str,
        rarity_hint: str | None = None,
        min_score: float = 0.58,
    ) -> tuple[MatchResult | None, dict[str, Any]]:
        """Evaluate a line once and return both `(MatchResult | None, diagnostics_dict)`."""
        norm = normalize_ocr_text(raw_text)
        if len(norm) < 2:
            return None, {
                "raw_text": raw_text,
                "normalized_text": norm,
                "matched": False,
                "ambiguous_rejected": False,
                "top1_augment_id": "",
                "top1_augment_name": "",
                "top1_score": 0.0,
                "top2_augment_id": "",
                "top2_augment_name": "",
                "top2_score": 0.0,
                "margin": 0.0,
                "seq_ratio": 0.0,
                "jaccard_ratio": 0.0,
                "substring_hit": False,
                "rarity_boosted": False,
                "is_near_miss": False,
                "near_miss_top1_id": "",
                "near_miss_top1_name": "",
                "near_miss_top1_score": 0.0,
            }

        (
            best_aug,
            best_score,
            second_aug,
            second_score,
            best_seq,
            best_jaccard,
            best_sub_hit,
            best_rarity_boost,
        ) = self._scan_candidates(norm, rarity_hint=rarity_hint)

        clamped_best = min(1.0, best_score) if best_aug is not None else 0.0
        clamped_top2 = min(1.0, second_score) if second_aug is not None else 0.0
        margin = round(max(0.0, clamped_best - clamped_top2), 4) if best_aug is not None else 0.0
        raw_matched = bool(best_aug is not None and best_score >= min_score)
        # Reject ambiguous 2-char prefix fragments (e.g. cursor covering "献祭" in "升级：献祭" -> "升级")
        # that tie across multiple longer augments, while preserving exact 2-char augments (score == 1.0).
        ambiguous_rejected = bool(
            raw_matched and len(norm) <= 2 and margin < 0.03 and clamped_best < 0.85
        )
        matched = bool(raw_matched and not ambiguous_rejected)
        is_near_miss = bool(
            best_aug is not None
            and not matched
            and not ambiguous_rejected
            and NEAR_MISS_MIN_SCORE <= clamped_best < min_score
        )
        near_miss_id = best_aug.augment_id if (is_near_miss and best_aug is not None) else ""
        near_miss_name = best_aug.name if (is_near_miss and best_aug is not None) else ""
        near_miss_score = round(clamped_best, 4) if is_near_miss else 0.0

        diag: dict[str, Any] = {
            "raw_text": raw_text,
            "normalized_text": norm,
            "matched": matched,
            "ambiguous_rejected": ambiguous_rejected,
            "top1_augment_id": best_aug.augment_id if best_aug is not None else "",
            "top1_augment_name": best_aug.name if best_aug is not None else "",
            "top1_score": round(clamped_best, 4),
            "top2_augment_id": second_aug.augment_id if second_aug is not None else "",
            "top2_augment_name": second_aug.name if second_aug is not None else "",
            "top2_score": round(clamped_top2, 4),
            "margin": margin,
            "seq_ratio": round(best_seq, 4),
            "jaccard_ratio": round(best_jaccard, 4),
            "substring_hit": best_sub_hit,
            "rarity_boosted": best_rarity_boost,
            "is_near_miss": is_near_miss,
            "near_miss_top1_id": near_miss_id,
            "near_miss_top1_name": near_miss_name,
            "near_miss_top1_score": near_miss_score,
        }

        if matched and best_aug is not None:
            result = MatchResult(
                augment=best_aug,
                score=clamped_best,
                raw_text=raw_text,
                top2_augment_id=second_aug.augment_id if second_aug is not None else "",
                top2_augment_name=second_aug.name if second_aug is not None else "",
                top2_score=round(clamped_top2, 4),
                margin=margin,
                seq_ratio=round(best_seq, 4),
                jaccard_ratio=round(best_jaccard, 4),
                substring_hit=best_sub_hit,
                rarity_boosted=best_rarity_boost,
                near_miss_top1_id="",
                near_miss_top1_score=0.0,
            )
            return result, diag
        return None, diag

    def best_candidate_diagnostics(
        self,
        raw_text: str,
        rarity_hint: str | None = None,
        min_score: float = 0.58,
    ) -> dict[str, Any]:
        """Return candidate top1/top2/near-miss diagnostic breakdown for any OCR line."""
        _, diag = self.match_line_with_diagnostics(raw_text, rarity_hint=rarity_hint, min_score=min_score)
        return diag

    def match_line(
        self,
        raw_text: str,
        rarity_hint: str | None = None,
        min_score: float = 0.58,
    ) -> MatchResult | None:
        result, _ = self.match_line_with_diagnostics(
            raw_text,
            rarity_hint=rarity_hint,
            min_score=min_score,
        )
        return result
