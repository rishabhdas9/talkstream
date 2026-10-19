from __future__ import annotations

import re

_MARKDOWN = (
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),  # [text](url) -> text
    (re.compile(r"(?m)^\s{0,3}(?:#{1,6}|[-*+]|>)\s+"), ""),  # headings, bullets, quotes
    (re.compile(r"\*\*|__|`+|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)"), ""),  # emphasis, code
)


def spoken_form(text: str) -> str:
    """Drops markdown the model emits anyway, so TTS never reads markup aloud."""
    for pattern, replacement in _MARKDOWN:
        text = pattern.sub(replacement, text)
    return text.strip()


class PhraseChunker:
    """Incrementally emits provider-safe speech phrases without any I/O."""

    SENTENCE_ENDS = frozenset(".!?।॥\n")
    CLAUSE_ENDS = frozenset(",;:،؛")

    def __init__(
        self,
        *,
        min_chars: int = 24,
        clause_min_chars: int | None = None,
        first_max_chars: int | None = None,
        max_chars: int = 180,
    ) -> None:
        clause_min_chars = (
            max(min_chars, min(48, max_chars)) if clause_min_chars is None else clause_min_chars
        )
        first_max_chars = min(80, max_chars) if first_max_chars is None else first_max_chars
        if (
            min_chars < 1
            or max_chars < min_chars
            or clause_min_chars < min_chars
            or clause_min_chars > max_chars
            or first_max_chars < min_chars
            or first_max_chars > max_chars
        ):
            raise ValueError("invalid phrase chunk bounds")
        self._min_chars = min_chars
        self._clause_min_chars = clause_min_chars
        self._first_max_chars = first_max_chars
        self._max_chars = max_chars
        self._buffer = ""
        self._emitted_any = False

    def push(self, text: str) -> list[str]:
        self._buffer += text
        chunks: list[str] = []
        while boundary := self._next_boundary():
            chunk = self._buffer[:boundary].strip()
            self._buffer = self._buffer[boundary:].lstrip()
            if self._is_speakable(chunk):
                self._emitted_any = True
                chunks.append(chunk)
        return chunks

    def flush(self) -> list[str]:
        remainder = self._buffer.strip()
        self._buffer = ""
        return [remainder] if self._is_speakable(remainder) else []

    @staticmethod
    def _is_speakable(text: str) -> bool:
        """Bulbul rejects chunks made only of punctuation or markup."""
        return any(character.isalnum() for character in text)

    def _next_boundary(self) -> int | None:
        active_max = self._max_chars if self._emitted_any else self._first_max_chars
        for index, character in enumerate(self._buffer[:active_max], start=1):
            # "4.2" and "1,000" are one number; wait to see the next character before splitting.
            if character in ".," and self._buffer[index - 2 : index - 1].isdigit():
                following = self._buffer[index : index + 1]
                if not following or following.isdigit():
                    continue
            if index >= self._min_chars and character in self.SENTENCE_ENDS:
                return index
            if index >= self._clause_min_chars and character in self.CLAUSE_ENDS:
                return index
        if len(self._buffer) <= active_max:
            return None
        whitespace = self._buffer.rfind(" ", self._min_chars, active_max + 1)
        return whitespace + 1 if whitespace >= self._min_chars else active_max


# Backward-compatible import for callers outside the voice session. The policy
# is now phrase-aware even though older code may still use the historical name.
SentenceChunker = PhraseChunker
