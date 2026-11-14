"""Search providers and per-game cached pages for guided play."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from rank_bm25 import BM25Plus

from .. import database
from ..setting import get_settings, setting


class SearchBackend(Protocol):
    def search(self, query: str, max_results: int = 5) -> list[dict]: ...

    def fetch(self, url: str) -> dict: ...


class SearchError(RuntimeError):
    """A provider failure that contains no credentials or response body."""


def valid_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (len(url) <= 8192 and parsed.scheme.lower() in {"http", "https"}
                and bool(parsed.hostname) and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


class DeepSeekSearchBackend:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise SearchError("Search credentials are not configured")
        self._api_key = api_key

    def _post(self, path: str, payload: dict) -> dict:
        try:
            response = httpx.post("https://api.deepseek.com" + path,
                                  headers={"Authorization": "Bearer " + self._api_key,
                                           "Content-Type": "application/json"},
                                  json=payload, timeout=20.0)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise TypeError("Invalid search response")
            return data
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise SearchError("Search provider request failed") from error

    def search(self, query: str, max_results: int = 5) -> list[dict]:
        data = self._post("/v1/tools/search", {"query": query, "count": max_results, "need_content": False})
        results = data.get("results")
        if not isinstance(results, list):
            raise SearchError("Invalid search response")
        return [{"title": str(row.get("title") or ""), "url": str(row["url"]),
                 "snippet": str(row.get("snippet") or row.get("summary") or "")}
                for row in results if isinstance(row, dict) and valid_url(str(row.get("url") or ""))]

    def fetch(self, url: str) -> dict:
        if not valid_url(url):
            raise SearchError("Invalid page URL")
        data = self._post("/v1/tools/fetch", {"url": url})
        if not isinstance(data.get("content"), str) or not isinstance(data.get("title"), str):
            raise SearchError("Invalid page response")
        return {"url": url, "title": data["title"], "content": data["content"][:8000]}


class MockSearchBackend:
    """A deterministic provider; it never uses the network."""

    def search(self, query: str, max_results: int = 5) -> list[dict]:
        return [{"title": "Example game rules", "url": "https://example.test/rules",
                 "snippet": "Example setup and turn rules."}][:max_results]

    def fetch(self, url: str) -> dict:
        if not valid_url(url):
            raise SearchError("Invalid page URL")
        return {"url": url, "title": "Example game rules",
                "content": "# Example rules\nSet up the game, then take turns."}


def enabled() -> bool:
    return get_settings().search_provider != "none"


def get_backend() -> SearchBackend | None:
    provider = get_settings().search_provider
    if provider == "none":
        return None
    if provider == "mock":
        return MockSearchBackend()
    key = setting("SEARCH_API_KEY") or setting("MODEL_API_KEY") or setting("DEEPSEEK_API_KEY")
    return DeepSeekSearchBackend(key)


def save_page(game_id: str, page: dict, lang: str) -> None:
    with closing(database.connect_runtime()) as connection, connection:
        connection.execute(
            "INSERT INTO web_cache (game_id, url, title, text, fetched_at, lang) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(game_id, url) DO UPDATE SET title=excluded.title, text=excluded.text, "
            "fetched_at=excluded.fetched_at, lang=excluded.lang",
            (game_id, page["url"], page["title"], page["content"][:8000], datetime.now(UTC).isoformat(), lang),
        )


def cached_pages(game_id: str) -> list[dict]:
    with closing(database.connect_runtime()) as connection:
        rows = connection.execute("SELECT url, title, text FROM web_cache WHERE game_id=? ORDER BY url",
                                  (game_id,)).fetchall()
    return [dict(row) for row in rows]


def search_cached_and_rulebook(game_id: str, query: str, k: int = 3) -> list[dict]:
    pages = cached_pages(game_id)
    if not pages:
        return database.search_rulebook(game_id, query, k)
    book = database.read_rulebook(game_id)
    chunks = [{"chunk": index, "text": text} for index, text in
              enumerate(database.chunk_text(book["text"]))] if book else []
    for page in pages:
        chunks.extend({"chunk": index, "text": text, "source": "web", "url": page["url"],
                       "title": page["title"]}
                      for index, text in enumerate(database.chunk_text(page["text"])))
    corpus = [database.tokenize(chunk["text"]) for chunk in chunks]
    if k <= 0 or not corpus or not any(corpus):
        return []
    scores = BM25Plus(corpus).get_scores(database.tokenize(query))
    indices = sorted(range(len(chunks)), key=lambda index: (-float(scores[index]), index))[:k]
    return [{**chunks[index], "score": float(scores[index])} for index in indices]


def event_sources(events: list[dict]) -> list[dict]:
    sources: dict[str, str] = {}
    for event in events:
        result = event.get("result")
        if isinstance(result, dict) and "error" in result:
            continue
        rows = (result if event.get("name") == "web_search" and isinstance(result, list) else
                [result] if event.get("name") == "fetch_page" and isinstance(result, dict) else
                result.get("passages", []) if event.get("name") == "lookup_rulebook"
                and isinstance(result, dict) else [])
        for row in rows:
            if isinstance(row, dict) and valid_url(str(row.get("url") or "")):
                sources[row["url"]] = str(row.get("title") or row["url"])
    return [{"title": title, "url": url} for url, title in sources.items()]
