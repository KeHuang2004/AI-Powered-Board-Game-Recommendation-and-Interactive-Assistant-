"""Test token estimates, chunking, retrieval, and context selection."""
import pytest

from backend import rulebook


def test_estimate_tokens_and_chunk_boundaries() -> None:
    assert rulebook.estimate_tokens("") == 0
    assert rulebook.estimate_tokens("中文，ABCD") == 3
    assert rulebook.chunk_text("") == []
    chunks = rulebook.chunk_text("a" * 801)
    assert [len(chunk) for chunk in chunks] == [800, 151]
    assert chunks[0][-150:] == chunks[1][:150]


def test_short_rulebook_context_contains_full_text() -> None:
    book = rulebook.read("blackjack")
    context = rulebook.context_for("blackjack")
    assert book and context
    assert book["est_tokens"] == rulebook.estimate_tokens(book["text"])
    assert context["is_excerpt"] is False
    assert context["text"] == book["text"]
    assert rulebook.context_for("not-a-game") is None
    assert rulebook.search("not-a-game", "setup") == []


def test_long_rulebook_uses_relevant_chunks() -> None:
    book = rulebook.read("uno")
    context = rulebook.context_for("uno")
    assert book and book["est_tokens"] > 12000
    assert context and context["is_excerpt"] is True
    assert len(context["chunks"]) == 8
    assert len(context["text"]) < len(book["text"])
    assert len(rulebook.search("uno", "scoring", 3)) == 3
    assert all(len(part["text"]) <= 800 for part in context["chunks"])


def test_search_ranks_matching_chunk_first(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "a" * 1000 + " zebra " + "b" * 1200
    monkeypatch.setattr(rulebook, "read", lambda game_id: {"text": text})
    result = rulebook.search("example", "zebra", 1)
    assert len(result) == 1
    assert "zebra" in result[0]["text"]
