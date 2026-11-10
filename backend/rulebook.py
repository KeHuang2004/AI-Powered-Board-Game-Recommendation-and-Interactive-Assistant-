"""On-demand rulebook text access and lexical retrieval."""
from __future__ import annotations

import math
import re
import sqlite3
from contextlib import closing

from rank_bm25 import BM25Okapi

from .catalog_db import DATABASE_PATH

CJK_PUNCTUATION = frozenset("，。！？；：、（）《》〈〉「」『』【】〔〕［］｛｝“”‘’—…·")
CHUNK_LENGTH = 800
CHUNK_OVERLAP = 150
FULL_CONTEXT_TOKENS = 12000


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0x20000 <= code <= 0x2FA1F
        or 0x3000 <= code <= 0x303F
        or char in CJK_PUNCTUATION
    )


def estimate_tokens(text: str) -> int:
    cjk = sum(_is_cjk(char) for char in text)
    return math.ceil(cjk / 1.5 + (len(text) - cjk) / 4)


def read(game_id: str) -> dict | None:
    with closing(sqlite3.connect(DATABASE_PATH.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute(
            "SELECT filename, text, est_tokens FROM rulebooks WHERE game_id = ?", (game_id,)
        ).fetchone()
    return {"filename": row[0], "text": row[1], "est_tokens": row[2]} if row else None


def chunk_text(text: str) -> list[str]:
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        chunk = text[start:start + CHUNK_LENGTH]
        chunks.append(chunk)
        if start + CHUNK_LENGTH >= len(text):
            break
        start += CHUNK_LENGTH - CHUNK_OVERLAP
    return chunks


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", text.casefold())


def search(game_id: str, query: str, k: int = 3) -> list[dict]:
    book = read(game_id)
    if book is None or k <= 0:
        return []
    chunks = chunk_text(book["text"])
    if not chunks:
        return []
    corpus = [_tokens(chunk) for chunk in chunks]
    if not any(corpus):
        return []
    scores = BM25Okapi(corpus).get_scores(_tokens(query))
    indices = sorted(range(len(chunks)), key=lambda i: (-float(scores[i]), i))[:k]
    return [{"chunk": i, "text": chunks[i], "score": float(scores[i])} for i in indices]


def context_for(game_id: str, k: int = 8) -> dict | None:
    book = read(game_id)
    if book is None:
        return None
    if book["est_tokens"] <= FULL_CONTEXT_TOKENS:
        return {"text": book["text"], "is_excerpt": False, "filename": book["filename"]}
    excerpts = search(game_id, "setup turn scoring", k)
    return {"text": "\n\n".join(item["text"] for item in excerpts),
            "is_excerpt": True, "filename": book["filename"], "chunks": excerpts}
