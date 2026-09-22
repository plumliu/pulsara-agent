"""Process-local deletion admission, never a durable recovery record."""

import asyncio
from dataclasses import dataclass

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard


@dataclass(eq=False, slots=True)
class KernelSessionDeletion:
    session_id: str
    memory_domain_id: str
    owner: object
    settlement: asyncio.Future[None]
    closed_writer: HostWriterGuard | None = None
    physical_full: bool = False
    quarantined: bool = False


class SessionDeleteRejected(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.public_code = code
        self.status = status
