"""Private, call-local values for the capability editor's user submission.

This is not a permission decision or a mutation receipt. The pending owner only
validates an exact user draft; the tool execution owner admits and executes it
after the submission has won the same-Host interaction slot.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from pulsara_agent.capability.mcp_management import McpSecretMutation
from pulsara_agent.primitives.context import FrozenJsonObjectFact
from pulsara_agent.mcp_credentials import binding_from_dict


def parse_mcp_secret_changes(value: object) -> tuple[McpSecretMutation, ...]:
    """The shared private HTTP/form decoder; never used for model arguments."""
    if not isinstance(value, list):
        raise ValueError("MCP secret changes must be a list")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"binding", "value"}:
            raise ValueError("invalid MCP secret change")
        secret = item["value"]
        if secret is not None and not isinstance(secret, str):
            raise ValueError("invalid MCP secret value")
        result.append(McpSecretMutation(binding_from_dict(item["binding"]), secret))
    if len({item.binding for item in result}) != len(result):
        raise ValueError("duplicate MCP secret change")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CapabilityFormValues:
    public_fields: FrozenJsonObjectFact
    secret_changes: tuple[McpSecretMutation, ...] = field(default=(), repr=False)
    retain_credentials_confirmed: bool = False
    enable_review_accepted: bool = False


class AcceptedCapabilityFormSubmission:
    """One transfer to execution; never copied into a live/canonical DTO."""

    __slots__ = ("_values",)

    def __init__(self, values: CapabilityFormValues) -> None:
        self._values: CapabilityFormValues | None = values

    def take(self) -> CapabilityFormValues:
        values = self._values
        if values is None:
            raise RuntimeError("capability submission was already consumed")
        self._values = None
        return values


@dataclass(frozen=True, slots=True)
class PendingCapabilityForm:
    # Prepared by the trusted target owner, not supplied by a model or browser.
    # The public projection contains only the shared editor's non-secret draft.
    public_projection: FrozenJsonObjectFact
    public_prompt: str
    prepare_submission: Callable[
        [Mapping[str, object]], Awaitable[CapabilityFormValues]
    ] = field(repr=False, compare=False)
