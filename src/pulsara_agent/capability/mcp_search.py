"""Disposable BM25 retrieval over the currently accepted MCP directory."""

from __future__ import annotations

import re

import bm25s


# Retain Unicode words and single Han characters through the SDK tokenizer.
_TOKEN_PATTERN = r"[\u3400-\u9fff]|[^\W\u3400-\u9fff]+"


def _tokens(texts, *, ids):
    return bm25s.tokenize(
        texts, token_pattern=_TOKEN_PATTERN, stopwords=[],
        return_ids=ids, show_progress=False,
    )


def _description_excerpt(description: str, query: str) -> str | None:
    terms = _tokens([query], ids=False)[0]
    if not description or not terms:
        return None
    match = re.search("|".join(re.escape(term) for term in terms), description, re.IGNORECASE)
    if match is None:
        return None
    # This is a display excerpt, never the schema or invocation's input range.
    start = max(0, match.start() - 120)
    end = min(len(description), max(match.end(), start + 400))
    return ("…" if start else "") + description[start:end] + ("…" if end < len(description) else "")


def search_mcp(query: str, *, tools, servers):
    """Return matching tools first, followed by service matches, within each
    group ordered by the library's BM25 score and stable directory order.

    Full schemas remain on the caller's accepted tool objects; only description
    display is excerpted. No persistent search index or ranking authority exists.
    """
    documents = []
    values = []
    for tool in tools:
        documents.append(f"{tool.remote_tool_name}\n{tool.description}")
        values.append(("tool", tool))
    for server in servers:
        documents.append(f"{server.server_id}\n{server.display_name}\n{server.sanitized_instructions or ''}")
        values.append(("service", server))
    if not documents:
        return []
    query_tokens = _tokens([query], ids=False)[0]
    if not query_tokens:
        return []
    retriever = bm25s.BM25()
    retriever.index(_tokens(documents, ids=True), show_progress=False)
    scores = retriever.get_scores(query_tokens)
    ranked = sorted(
        (index for index, score in enumerate(scores) if score > 0),
        key=lambda index: (values[index][0] != "tool", -float(scores[index]), index),
    )
    return [
        (kind, item, _description_excerpt(item.description, query) if kind == "tool" else None)
        for index in ranked
        for kind, item in (values[index],)
    ]
