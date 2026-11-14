"""Test token estimates, chunking, retrieval, and context selection."""
import pytest

from backend import database


def test_estimate_tokens_and_chunk_boundaries() -> None:
    assert database.estimate_tokens("") == 0
    assert database.estimate_tokens("中文，ABCD") == 3
    assert database.chunk_text("") == []
    chunks = database.chunk_text("a" * 801)
    assert [len(chunk) for chunk in chunks] == [800, 151]
    assert chunks[0][-150:] == chunks[1][:150]


def test_short_rulebook_context_contains_full_text() -> None:
    book = database.read_rulebook("blackjack")
    context = database.rulebook_context("blackjack")
    assert book and context
    assert book["est_tokens"] == database.estimate_tokens(book["text"])
    assert context["is_excerpt"] is False
    assert context["text"] == book["text"]
    assert database.rulebook_context("not-a-game") is None
    assert database.search_rulebook("not-a-game", "setup") == []


def test_long_rulebook_uses_relevant_chunks() -> None:
    book = database.read_rulebook("uno")
    context = database.rulebook_context("uno")
    assert book and book["est_tokens"] > 12000
    assert context and context["is_excerpt"] is True
    assert len(context["chunks"]) == 8
    assert len(context["text"]) < len(book["text"])
    assert len(database.search_rulebook("uno", "scoring", 3)) == 3
    assert all(len(part["text"]) <= 800 for part in context["chunks"])


def test_search_ranks_matching_chunk_first(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "a" * 1000 + " zebra " + "b" * 1200
    monkeypatch.setattr(database, "read_rulebook", lambda game_id: {"text": text})
    result = database.search_rulebook("example", "zebra", 1)
    assert len(result) == 1
    assert "zebra" in result[0]["text"]
