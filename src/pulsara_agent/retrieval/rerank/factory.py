"""Build the selected frozen rerank or decision connection."""

from .http import HttpRerankProvider
from .decision import DecisionRerankProvider
from pulsara_agent.retrieval.config import RerankBackendConfig


def build_rerank_provider(
    connection, *, config=RerankBackendConfig(), semaphore=None, decision_service=None
):
    if connection.shape in {"system_one", "openai_decisions"}:
        return DecisionRerankProvider(connection, decision_service=decision_service)
    return HttpRerankProvider(connection, config=config, semaphore=semaphore)
