"""LLM model identity and roles."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pulsara_agent.llm.provider import ProviderProfile


class ModelRole(StrEnum):
    PRO = "pro"
    FLASH = "flash"


@dataclass(frozen=True, slots=True)
class ModelProfile:
    id: str
    role: ModelRole
    api: str
    provider: str
    base_url: str
    provider_profile: ProviderProfile = field(default_factory=ProviderProfile)
    supports_tools: bool = True
    supports_reasoning: bool = True
    context_window: int | None = None
    max_output_tokens: int | None = None
