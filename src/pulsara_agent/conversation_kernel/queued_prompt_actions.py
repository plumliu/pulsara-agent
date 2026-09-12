"""Exact PR04 action values; session_commands owns durable idempotency."""

from dataclasses import dataclass

from pulsara_agent.conversation_kernel.contracts import canonical_digest
from pulsara_agent.conversation_kernel.steer import _stable_id


class QueuedPromptActionRejected(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class QueuedPromptAction:
    session_id: str
    command_id: str
    source_queue_item_id: str
    target_turn_id: str | None = None

    def __post_init__(self) -> None:
        if not all((self.session_id, self.command_id, self.source_queue_item_id)):
            raise ValueError("queue action identity is required")
        if self.target_turn_id == "":
            raise ValueError("steer target must be an exact turn")

    @property
    def command_kind(self) -> str:
        return "STEER_QUEUED_PROMPT" if self.target_turn_id else "CANCEL_PROMPT"

    @property
    def replacement_queue_item_id(self) -> str | None:
        if self.target_turn_id is None:
            return None
        # The same canonical ID builder used by ordinary prompt ingress.
        return _stable_id("queue-item", self.session_id, self.command_id)

    @property
    def target_queue_item_id(self) -> str:
        return self.replacement_queue_item_id or self.source_queue_item_id

    @property
    def semantic_digest(self) -> str:
        return canonical_digest(
            "pulsara:queued-prompt-action:v1",
            {
                "command_kind": self.command_kind,
                "source_queue_item_id": self.source_queue_item_id,
                "replacement_queue_item_id": self.replacement_queue_item_id,
                "target_turn_id": self.target_turn_id,
            },
        )
