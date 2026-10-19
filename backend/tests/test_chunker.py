from __future__ import annotations

import pytest

from app.voice.chunker import PhraseChunker, SentenceChunker


def test_buffers_until_sentence_is_long_enough() -> None:
    chunker = SentenceChunker(min_chars=10, max_chars=30)

    assert chunker.push("Short. ") == []
    assert chunker.push("Enough now.") == ["Short. Enough now."]


def test_splits_devanagari_danda_and_newline() -> None:
    chunker = SentenceChunker(min_chars=4, max_chars=50)

    assert chunker.push("नमस्ते। अर्को\n") == ["नमस्ते।", "अर्को"]


def test_max_length_prefers_word_boundary() -> None:
    chunker = SentenceChunker(min_chars=5, max_chars=12)

    assert chunker.push("alpha beta gamma delta") == ["alpha beta"]
    assert chunker.flush() == ["gamma delta"]


def test_flush_returns_remainder_once() -> None:
    chunker = SentenceChunker()
    chunker.push("unfinished")

    assert chunker.flush() == ["unfinished"]
    assert chunker.flush() == []


def test_drops_chunks_without_letters_or_numbers() -> None:
    chunker = SentenceChunker(min_chars=1, max_chars=30)

    assert chunker.push("**...**\n") == []
    assert chunker.push("Hello!\n") == ["Hello!"]
    assert chunker.flush() == []


def test_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError):
        SentenceChunker(min_chars=0)


def test_first_phrase_releases_at_safe_clause_boundary() -> None:
    chunker = PhraseChunker(
        min_chars=24,
        clause_min_chars=48,
        first_max_chars=80,
        max_chars=180,
    )

    assert chunker.push(
        "This answer starts with enough useful context for speech, "
        "then continues without a full stop"
    ) == ["This answer starts with enough useful context for speech,"]
    assert chunker.flush() == ["then continues without a full stop"]


def test_first_phrase_cap_prefers_whitespace_and_preserves_streamed_text() -> None:
    chunker = PhraseChunker(
        min_chars=20,
        clause_min_chars=50,
        first_max_chars=54,
        max_chars=120,
    )
    tokens = ["A long ", "streamed response ", "without punctuation keeps ", "going naturally"]

    emitted = [chunk for token in tokens for chunk in chunker.push(token)]
    emitted.extend(chunker.flush())

    assert emitted[0] == "A long streamed response without punctuation keeps"
    assert " ".join(emitted) == "A long streamed response without punctuation keeps going naturally"


def test_first_phrase_cap_wins_when_sentence_punctuation_arrives_later_in_one_push() -> None:
    chunker = PhraseChunker(
        min_chars=20,
        clause_min_chars=48,
        first_max_chars=60,
        max_chars=180,
    )

    chunks = chunker.push(
        "This first phrase must stop near its configured cap even when the model sends "
        "a much later sentence ending in the same token."
    )

    assert chunks[0] == "This first phrase must stop near its configured cap even"
    assert len(chunks[0]) <= 60


def test_phrase_chunker_supports_indic_clause_punctuation() -> None:
    chunker = PhraseChunker(
        min_chars=8,
        clause_min_chars=12,
        first_max_chars=40,
        max_chars=80,
    )

    assert chunker.push("यह पर्याप्त लंबा खंड है، आगे जारी है।") == [
        "यह पर्याप्त लंबा खंड है،",
        "आगे जारी है।",
    ]


def test_spoken_form_strips_markdown_but_keeps_words() -> None:
    from app.voice.chunker import spoken_form

    assert (
        spoken_form("1. **Waffle Magic** — open until 3 AM,")
        == "1. Waffle Magic — open until 3 AM,"
    )
    assert spoken_form("- *Chelvies* in [Indiranagar](https://x.test)") == "Chelvies in Indiranagar"
    assert spoken_form("## Top picks:\n- `Blue Tokai`") == "Top picks:\nBlue Tokai"
    assert spoken_form("5 * 3 = 15, snake_case stays") == "5 * 3 = 15, snake_case stays"
    assert spoken_form("**") == ""


def test_decimals_and_thousands_are_not_split() -> None:
    chunker = PhraseChunker(min_chars=4, clause_min_chars=4)
    text = "Neon Market is rated 4.8 and costs about 1,200 rupees. It opens at 9."
    assert chunker.push(text) == ["Neon Market is rated 4.8 and costs about 1,200 rupees."]
    assert chunker.flush() == ["It opens at 9."]
    streamed = PhraseChunker(min_chars=4)
    assert streamed.push("It has a rating of 4.") == []
    assert streamed.push("2 overall.") == ["It has a rating of 4.2 overall."]
