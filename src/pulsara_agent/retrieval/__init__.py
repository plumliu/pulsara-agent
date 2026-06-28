"""Retrieval-side model services.

This package follows the useful part of EverOS's ``component`` layout:

- small provider protocols
- one factory per capability
- provider-specific implementation files

But Pulsara keeps it narrower. We already have ``llm/`` for chat/reasoning, so
this package is only for retrieval-oriented services such as embedding and
rerank.
"""

from pulsara_agent.retrieval.config import (
    DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL,
    DEFAULT_DASHSCOPE_RERANK_BASE_URL,
    EmbeddingBackendConfig,
    RetrievalConfig,
    RerankBackendConfig,
    TokenizerBackendConfig,
)
from pulsara_agent.retrieval.embedding import (
    EmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    build_embedding_provider,
)
from pulsara_agent.retrieval.errors import (
    EmbeddingServiceError,
    RetrievalServiceError,
    RerankServiceError,
)
from pulsara_agent.retrieval.rerank import (
    DashScopeRerankProvider,
    RerankProvider,
    RerankResult,
    build_rerank_provider,
)
from pulsara_agent.retrieval.tokenizer import (
    JiebaSearchTokenizer,
    RegexWordSplitTokenizer,
    Tokenizer,
    build_tokenizer,
)

__all__ = [
    "DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL",
    "DEFAULT_DASHSCOPE_RERANK_BASE_URL",
    "DashScopeRerankProvider",
    "EmbeddingBackendConfig",
    "EmbeddingProvider",
    "EmbeddingServiceError",
    "OpenAICompatibleEmbeddingProvider",
    "RetrievalConfig",
    "RetrievalServiceError",
    "RerankBackendConfig",
    "RerankProvider",
    "RerankResult",
    "RerankServiceError",
    "Tokenizer",
    "TokenizerBackendConfig",
    "JiebaSearchTokenizer",
    "RegexWordSplitTokenizer",
    "build_embedding_provider",
    "build_rerank_provider",
    "build_tokenizer",
]
