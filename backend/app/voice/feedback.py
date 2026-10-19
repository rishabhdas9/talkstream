from __future__ import annotations

CUE_LANGUAGES = frozenset(
    {
        "bn-IN",
        "en-IN",
        "gu-IN",
        "hi-IN",
        "kn-IN",
        "ml-IN",
        "mr-IN",
        "od-IN",
        "pa-IN",
        "ta-IN",
        "te-IN",
    }
)


class ResponseCuePolicy:
    """Session-local, deterministic cue eligibility and language fallback."""

    def __init__(self, *, enabled: bool, cooldown_turns: int) -> None:
        if cooldown_turns < 0:
            raise ValueError("cue cooldown must be non-negative")
        self._enabled = enabled
        self._cooldown_turns = cooldown_turns
        self._last_reserved_turn_index: int | None = None

    def reserve(self, *, turn_index: int) -> bool:
        if not self._enabled or turn_index < 1:
            return False
        previous = self._last_reserved_turn_index
        if previous is not None and turn_index - previous <= self._cooldown_turns:
            return False
        self._last_reserved_turn_index = turn_index
        return True

    @staticmethod
    def language_for(language_code: str | None) -> str:
        return language_code if language_code in CUE_LANGUAGES else "neutral"
