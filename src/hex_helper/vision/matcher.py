"""237-augment constrained Chinese fuzzy dictionary matcher with rarity boost."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from hex_helper.data.provider import AugmentInfo

_STRIP_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")


def normalize_ocr_text(text: str) -> str:
    return _STRIP_RE.sub("", str(text or "")).lower()


@dataclass(frozen=True)
class MatchResult:
    augment: AugmentInfo
    score: float
    raw_text: str


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

    def match_line(
        self,
        raw_text: str,
        rarity_hint: str | None = None,
        min_score: float = 0.58,
    ) -> MatchResult | None:
        norm = normalize_ocr_text(raw_text)
        if len(norm) < 2:
            return None

        best_aug: AugmentInfo | None = None
        best_score = 0.0
        norm_chars = set(norm)

        for aug, cand_norm, cand_chars in self._entries:
            if norm == cand_norm:
                score = 1.0 + (0.05 if rarity_hint and aug.rarity == rarity_hint else 0.0)
            else:
                # Reject wild length mismatch unless one clearly contains the other
                len_ratio = min(len(norm), len(cand_norm)) / max(len(norm), len(cand_norm))
                if len_ratio < 0.45 and cand_norm not in norm:
                    continue

                seq = SequenceMatcher(None, norm, cand_norm).ratio()
                union = norm_chars | cand_chars
                jaccard = (len(norm_chars & cand_chars) / len(union)) if union else 0.0
                score = 0.65 * seq + 0.35 * jaccard

                if cand_norm in norm and len(cand_norm) >= 2:
                    score = max(score, 0.82 + 0.1 * (len(cand_norm) / len(norm)))
                if rarity_hint and aug.rarity == rarity_hint:
                    score += 0.12

            if score > best_score:
                best_score = score
                best_aug = aug

        if best_aug is not None and best_score >= min_score:
            return MatchResult(augment=best_aug, score=min(1.0, best_score), raw_text=raw_text)
        return None
