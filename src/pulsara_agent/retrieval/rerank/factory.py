"""Build the selected frozen rerank or decision connection."""

from .http import HttpRerankProvider
from pulsara_agent.retrieval.config import RerankBackendConfig


def build_rerank_provider(connection, *, config=RerankBackendConfig(), semaphore=None):
    return HttpRerankProvider(connection, config=config, semaphore=semaphore)
