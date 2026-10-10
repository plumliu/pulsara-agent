"""The single pg_search 0.26.1 lexical configuration for both corpora.

pg_search owns analysis, matching, BM25 and original UTF-8 hit positions.
Generated analysis columns adapt its physical-column-only snippet API; they
are disposable copies, never an additional canonical content owner.
"""

PG_SEARCH_VERSION = "0.26.1"


def lexical_predicate(column: str) -> str:
    return (
        "("
        + " OR ".join(
            f"{name} ||| %s" for name in (column, column + "_en", column + "_tech")
        )
        + ")"
    )


def lexical_parameters(query: str) -> tuple[str, str, str]:
    return query, query, query
