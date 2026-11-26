"""Coach prompt payload builder and item-grounding helpers for HexHelper."""

from .gemini_coach import (
    COACH_SYSTEM_INSTRUCTION,
    PICKER_FAST_SYSTEM_INSTRUCTION,
    GeminiCallStats,
    build_coach_prompt_payload,
    build_picker_fast_prompt_payload,
    ground_coach_advice,
)

__all__ = [
    "COACH_SYSTEM_INSTRUCTION",
    "PICKER_FAST_SYSTEM_INSTRUCTION",
    "GeminiCallStats",
    "build_coach_prompt_payload",
    "build_picker_fast_prompt_payload",
    "ground_coach_advice",
]
