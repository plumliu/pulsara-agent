"""The single pg_search 0.26.1 lexical configuration for both corpora.

pg_search owns analysis, matching, BM25 and original UTF-8 hit positions.
Generated analysis columns adapt its physical-column-only snippet API; they
are disposable copies, never an additional canonical content owner.
"""

import string

from psycopg.types.json import Jsonb

PG_SEARCH_VERSION = "0.26.1"

# Pulsara owns this small query vocabulary; pg_search owns tokenization.
# Keep negation, modality, conditions, quantities, time and speakers searchable.
_STOPWORDS_ZH = frozenset(
    "的 地 了 着 著 啊 呀 呢 吧 嘛 吗 麼 么 哦 喔 嗯 呃 哎 唉 啦 呗 嘞 喽 噢 哟 唷 嘿 呵 哇 和 与 與 及 而 是".split()
)
_STOPWORDS_EN = frozenset(
    "a an the of and to in for by as on with is are am be been being was were please".split()
)
_STOPWORDS = tuple(
    sorted(
        _STOPWORDS_ZH
        | _STOPWORDS_EN
        | set(string.punctuation + " \t\n\r，。！？；：、（）【】《》“”‘’—…")
    )
)
_TOKENIZERS = (
    {"type": "jieba", "search_mode": True, "stopwords": _STOPWORDS},
    # English analysis applies stopwords after stemming: please -> pleas.
    {"type": "default", "stemmer": "English", "stopwords": (*_STOPWORDS, "pleas")},
    {
        "type": "regex",
        "pattern": "[./:@+-]*[A-Za-z0-9_][A-Za-z0-9_./:@+-]*",
        "stopwords": _STOPWORDS,
    },
)


def lexical_predicate(column: str) -> str:
    return (
        "("
        + " OR ".join(
            f"{name} @@@ pdb.match(%s, tokenizer => %s)"
            for name in (column, column + "_en", column + "_tech")
        )
        + ")"
    )


def lexical_parameters(query: str) -> tuple:
    # Match and snippet reads share this exact analyzer. pg_search owns all
    # tokenization and original byte positions; canonical text stays untouched.
    return tuple(
        value for tokenizer in _TOKENIZERS for value in (query, Jsonb(tokenizer))
    )
