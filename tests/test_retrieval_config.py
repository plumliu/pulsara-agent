from __future__ import annotations

from pulsara_agent.retrieval import (
    DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL,
    DEFAULT_DASHSCOPE_RERANK_BASE_URL,
    DashScopeRerankProvider,
    EmbeddingBackendConfig,
    JiebaSearchTokenizer,
    OpenAICompatibleEmbeddingProvider,
    RegexWordSplitTokenizer,
    RerankBackendConfig,
    RetrievalConfig,
    TokenizerBackendConfig,
    build_embedding_provider,
    build_rerank_provider,
    build_tokenizer,
)


def test_retrieval_config_from_env_falls_back_to_shared_api_key(monkeypatch) -> None:
    monkeypatch.setenv("PULSARA_API_KEY", "shared-key")
    monkeypatch.delenv("PULSARA_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("PULSARA_RERANK_API_KEY", raising=False)

    config = RetrievalConfig.from_env()

    assert config.embedding.api_key == "shared-key"
    assert config.rerank.api_key == "shared-key"
    assert config.embedding.base_url == DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL
    assert config.rerank.base_url == DEFAULT_DASHSCOPE_RERANK_BASE_URL
    assert config.tokenizer.provider == "jieba_search"


def test_embedding_factory_builds_openai_compatible_provider() -> None:
    provider = build_embedding_provider(
        EmbeddingBackendConfig(
            api_key="test-key",
            base_url="https://example.com/v1",
            model="text-embedding-v4",
            dimensions=768,
        )
    )

    assert isinstance(provider, OpenAICompatibleEmbeddingProvider)
    assert provider.dimensions == 768


def test_rerank_factory_builds_dashscope_provider() -> None:
    provider = build_rerank_provider(
        RerankBackendConfig(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com",
            model="qwen3-rerank",
        )
    )

    assert isinstance(provider, DashScopeRerankProvider)


def test_tokenizer_factory_builds_jieba_search_provider() -> None:
    provider = build_tokenizer(
        TokenizerBackendConfig(
            provider="jieba_search",
            min_token_length=2,
            lowercase=True,
        )
    )

    assert isinstance(provider, JiebaSearchTokenizer)


def test_tokenizer_factory_builds_regex_word_split_provider() -> None:
    provider = build_tokenizer(
        TokenizerBackendConfig(
            provider="regex_word_split",
            min_token_length=1,
            lowercase=True,
        )
    )

    assert isinstance(provider, RegexWordSplitTokenizer)


def test_embedding_factory_requires_api_key() -> None:
    try:
        build_embedding_provider(
            EmbeddingBackendConfig(
                api_key="",
                base_url="https://example.com/v1",
                model="text-embedding-v4",
            )
        )
    except ValueError as exc:
        assert "api_key" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_rerank_factory_requires_api_key() -> None:
    try:
        build_rerank_provider(
            RerankBackendConfig(
                api_key="",
                base_url="https://dashscope.aliyuncs.com",
                model="qwen3-rerank",
            )
        )
    except ValueError as exc:
        assert "api_key" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
