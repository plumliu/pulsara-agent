"""Memory utility questions and ranking over the shared Decision service."""

from pulsara_agent.retrieval.decision import (
    DecisionError,
    DecisionQuestion,
    DecisionRequest,
    DecisionService,
)
from pulsara_agent.retrieval.errors import RerankServiceError
from .decision_prompt import decision_question
from .protocol import RerankPurpose, RerankResult


class DecisionRerankProvider:
    def __init__(self, connection, *, decision_service=None):
        if (
            connection.shape not in {"system_one", "openai_decisions"}
            or not connection.complete
        ):
            raise ValueError("decision configuration is invalid")
        self.connection = connection
        self.model_id = connection.model_id
        self._owns_service = decision_service is None
        self._service = (
            DecisionService() if decision_service is None else decision_service
        )

    async def aclose(self):
        if self._owns_service:
            await self._service.aclose()

    async def rerank(
        self, query, documents, *, candidate_ids=None, purpose: RerankPurpose = "recall"
    ):
        documents = tuple(documents)
        if not documents:
            return []
        # This is memory's existing shortlist boundary, not a Decision-call cap.
        if len(documents) > 20:
            raise RerankServiceError("rerank candidate count exceeds 20")
        names = (
            tuple(candidate_ids)
            if candidate_ids is not None
            else tuple(f"m{i}" for i in range(len(documents)))
        )
        if (
            len(names) != len(documents)
            or any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != len(names)
        ):
            raise RerankServiceError("candidate identities are invalid")

        questions = tuple(
            DecisionQuestion(name, *decision_question(name, purpose)) for name in names
        )

        def build(start, end):
            return DecisionRequest(
                state={
                    "user_task": query,
                    "candidates": [
                        {
                            "candidate_number": names[index],
                            "candidate": documents[index],
                        }
                        for index in range(start, end)
                    ],
                },
                questions=questions[start:end],
            )

        try:
            batches = self._service.prepare_batches(
                self.connection, item_count=len(documents), build_request=build
            )
            probabilities = await self._service.evaluate(batches)
        except DecisionError as exc:
            raise RerankServiceError(str(exc)) from exc
        return sorted(
            (RerankResult(index, score) for index, score in enumerate(probabilities)),
            key=lambda row: (-row.score, row.index),
        )
