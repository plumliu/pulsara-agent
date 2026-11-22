"""Hybrid retrieval over the currently accepted MCP directory."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
import re

import bm25s

from pulsara_agent.retrieval.ranking import reciprocal_rank_score


# Retain Unicode words and single Han characters through the SDK tokenizer.
_TOKEN_PATTERN = r"[\u3400-\u9fff]|[^\W\u3400-\u9fff]+"

# Operational default for MCP search, pending tool-sample calibration.
_PROVISIONAL_MCP_DENSE_COSINE_FLOOR = 0.40


def mcp_tool_search_text(tool) -> str:
    return tool.remote_tool_name + "\n" + (tool.description or "")


def _tokens(texts, *, ids):
    return bm25s.tokenize(
        texts, token_pattern=_TOKEN_PATTERN, stopwords=[],
        return_ids=ids, show_progress=False,
    )


def _bm25_match_indexes(documents: list[str], terms: list[str]) -> list[int]:
    if not documents or not terms:
        return []
    tokenized = _tokens(documents, ids=True)
    if not tokenized.vocab:
        return []
    retriever = bm25s.BM25()
    retriever.index(tokenized, show_progress=False)
    scores = retriever.get_scores(terms)
    return sorted(
        (index for index, score in enumerate(scores) if score > 0),
        key=lambda index: -float(scores[index]),
    )


def mcp_tool_description_excerpt(
    description: str | None, pattern: re.Pattern[str] | None = None,
) -> str | None:
    if not description:
        return None
    match = pattern.search(description) if pattern is not None else None
    start = max(0, match.start() - 120) if match is not None else 0
    end = min(len(description), start + 400)
    if match is not None:
        end = max(end, match.end())
    return ("…" if start else "") + description[start:end] + ("…" if end < len(description) else "")


def search_mcp(
    query: str,
    *,
    tools,
    servers,
    dense_similarities: Mapping[tuple[str, str], float] | None = None,
):
    """Return exact names, fused tool matches, then lexical service matches.

    The caller supplies cosine similarities for the current tool texts, keyed
    by server ID and remote tool name. The supplied directory resolves ties.
    """
    tools_by_key = {(tool.server_id, tool.remote_tool_name): tool for tool in tools}
    tool_keys = tuple(tools_by_key)
    servers = tuple(servers)
    tool_count = len(tool_keys)
    documents = [mcp_tool_search_text(tool) for tool in tools_by_key.values()]
    documents.extend(
        f"{server.server_id}\n{server.display_name}\n{server.sanitized_instructions or ''}"
        for server in servers
    )
    terms = _tokens([query], ids=False)[0]
    lexical_indexes = _bm25_match_indexes(documents, terms)
    lexical_ranks = {
        tool_keys[index]: rank
        for rank, index in enumerate((index for index in lexical_indexes if index < tool_count), 1)
    }

    similarities = dense_similarities or {}
    dense_keys = []
    for key in tool_keys:
        similarity = similarities.get(key)
        if (
            similarity is not None
            and isfinite(similarity)
            and similarity >= _PROVISIONAL_MCP_DENSE_COSINE_FLOOR
        ):
            dense_keys.append(key)
    dense_keys.sort(key=similarities.__getitem__, reverse=True)
    dense_ranks = {key: rank for rank, key in enumerate(dense_keys, 1)}

    exact_keys = [key for key in tool_keys if key[1] == query]
    if dense_ranks:
        fused_keys = sorted(
            (key for key in tool_keys if key[1] != query and (key in lexical_ranks or key in dense_ranks)),
            key=lambda key: reciprocal_rank_score(
                lexical_ranks.get(key), dense_ranks.get(key), first_weight=0.1, second_weight=1.0
            ),
            reverse=True,
        )
    else:
        fused_keys = [key for key in lexical_ranks if key[1] != query]
    pattern = (
        re.compile("|".join(re.escape(term) for term in terms), re.IGNORECASE)
        if terms and (exact_keys or fused_keys)
        else None
    )
    matches = [
        ("tool", tools_by_key[key], mcp_tool_description_excerpt(tools_by_key[key].description, pattern))
        for key in (*exact_keys, *fused_keys)
    ]
    matches.extend(
        ("service", servers[index - tool_count], None)
        for index in lexical_indexes
        if index >= tool_count
    )
    return matches
