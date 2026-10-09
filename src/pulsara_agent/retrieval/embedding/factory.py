"""Build a frozen embedding connection for one operation."""

from .openai_compatible import HttpEmbeddingProvider
from pulsara_agent.retrieval.config import EmbeddingBackendConfig


def build_embedding_provider(
    connection, *, config=EmbeddingBackendConfig(), semaphore=None
):
    return HttpEmbeddingProvider(connection, config=config, semaphore=semaphore)
