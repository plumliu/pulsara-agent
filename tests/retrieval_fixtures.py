"""Neutral retrieval fixtures shared by settings and kernel integration tests."""

from pulsara_agent.retrieval.config import MemoryRetrievalSettings, RetrievalConnection


def connection(slot, key=None, *, model=None, shape=None):
    return RetrievalConnection(
        f"https://provider.example/v1/{'embeddings' if slot == 'embedding' else 'rerank'}",
        model or f"{slot}-fixture",
        shape
        or {
            "embedding": "openai_embedding",
            "rerank": "flat_rerank",
            "decision": "system_one",
        }[slot],
        "bearer_api_key" if key is not None else "none",
        key,
    )


def retrieval_settings(embedding_key=None, rerank_key=None):
    return MemoryRetrievalSettings(
        embedding=None
        if embedding_key is None
        else connection("embedding", embedding_key),
        embedding_enabled=embedding_key is not None,
        rerank=None if rerank_key is None else connection("rerank", rerank_key),
        ranking_mode="rerank" if rerank_key is not None else "off",
    )


def input_for(slot, key=None, *, activate=False, previous=None, model=None, shape=None):
    config = connection(slot, key, model=model, shape=shape)
    raw = config.public()
    raw.pop("credential_configured")
    return {
        "connection": raw,
        "key_action": "replace" if key is not None else "clear",
        **({"api_key": key} if key is not None else {}),
        "activate": activate,
        **(
            {
                "confirm_reembed": True,
                "expected_embedding": None
                if previous is None
                else {
                    name: getattr(previous, name)
                    for name in ("endpoint", "model_id", "shape")
                },
            }
            if slot == "embedding"
            else {}
        ),
    }


async def save_model(store, slot, key=None, *, activate=False, model=None):
    previous = store.read().memory_retrieval.embedding
    return await store.save_retrieval_connection(
        slot, input_for(slot, key, activate=activate, previous=previous, model=model)
    )
