"""Process-local owner for one MCP ``inputRequests`` round.

Partial form values, exact URLs, and response values never leave this owner as
ordinary Python mappings.  Only a complete, canonical response set can be
frozen for the durable resolution transaction.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from enum import StrEnum
from threading import RLock
from typing import Mapping
from uuid import uuid4

from pulsara_agent.ports.mcp_elicitation import (
    McpConfirmedUrlLaunchAuthority,
    McpExternalBrowserPort,
    McpUrlLaunchDisposition,
    McpUrlLaunchOutcome,
)
from pulsara_agent.ports.mcp_secret import (
    McpElicitationAction,
    McpElicitationResponse,
    McpFormElicitationResponse,
    McpFrozenRoundInputResponses,
    McpPrivateUrlElicitationPayload,
    McpSealedElicitationResponseFactory,
    McpUrlElicitationResponse,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.mcp_continuation import (
    McpElicitationRequestFact,
    McpFormElicitationRequestFact,
    McpUrlElicitationRequestFact,
)


class McpElicitationBatchState(StrEnum):
    COLLECTING = "collecting"
    RESOLUTION_READY = "resolution_ready"
    COMMITTING = "committing"
    FULL = "full"
    ABORTING = "aborting"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    RETIRED = "retired"


class McpFormItemState(StrEnum):
    AWAITING_FORM_RESPONSE = "awaiting_form_response"
    TERMINAL_RESPONSE_FROZEN = "terminal_response_frozen"


class McpUrlItemState(StrEnum):
    AWAITING_URL_CONSENT = "awaiting_url_consent"
    LAUNCHING = "launching"
    AWAITING_URL_RETRY = "awaiting_url_retry"
    TERMINAL_RESPONSE_FROZEN = "terminal_response_frozen"


@dataclass(frozen=True, slots=True)
class McpFormItemSlot:
    request: McpFormElicitationRequestFact
    state: McpFormItemState
    response: McpFormElicitationResponse | None


@dataclass(frozen=True, slots=True)
class McpUrlItemSlot:
    request: McpUrlElicitationRequestFact
    private_url_payload_fingerprint: str
    state: McpUrlItemState
    launch_attempt_generation: int
    response: McpUrlElicitationResponse | None
    sanitized_launch_diagnostic: str | None = None


McpElicitationItemSlot = McpFormItemSlot | McpUrlItemSlot


@dataclass(frozen=True, slots=True)
class McpElicitationBatchIdentity:
    owner_id: str
    runtime_session_id: str
    interaction_id: str
    round_ordinal: int
    request_set_fingerprint: str
    ordered_request_keys: tuple[str, ...]
    owner_generation: int

    def __post_init__(self) -> None:
        if (
            not self.owner_id
            or not self.runtime_session_id
            or not self.interaction_id
            or self.round_ordinal < 1
            or self.owner_generation < 1
            or not self.request_set_fingerprint
        ):
            raise ValueError("MCP elicitation batch identity is incomplete")
        if self.ordered_request_keys != tuple(
            sorted(set(self.ordered_request_keys))
        ) or not self.ordered_request_keys:
            raise ValueError("MCP elicitation request keys must be ordered and unique")


@dataclass(frozen=True, slots=True)
class McpRecoveredResolvedBatchOwner:
    """Tombstone owner for a resolution already committed before restart."""

    identity: McpElicitationBatchIdentity
    item_slots: tuple[McpElicitationItemSlot, ...]

    @property
    def state(self) -> McpElicitationBatchState:
        return McpElicitationBatchState.RETIRED

    def retire(self) -> None:
        return

    def exact_url_for_display(self, *, request_key: str) -> str:
        del request_key
        raise RuntimeError("resolved MCP batch no longer owns a displayable URL")


class McpElicitationBatchOwner:
    """Single mutation and physical-operation owner for one elicitation round."""

    __slots__ = (
        "identity",
        "_state",
        "_slots",
        "_private_urls",
        "_response_factory",
        "_browser",
        "_frozen_resolution",
        "_physical_tasks",
        "_lock",
    )

    def __init__(
        self,
        *,
        identity: McpElicitationBatchIdentity,
        requests: tuple[McpElicitationRequestFact, ...],
        private_url_payloads: tuple[McpPrivateUrlElicitationPayload, ...],
        response_factory: McpSealedElicitationResponseFactory,
        browser_port: McpExternalBrowserPort | None,
    ) -> None:
        request_keys = tuple(item.key for item in requests)
        if request_keys != identity.ordered_request_keys:
            raise ValueError("MCP batch request order differs from its identity")
        private_by_key = {item.request_key: item for item in private_url_payloads}
        expected_private = {
            item.key for item in requests if isinstance(item, McpUrlElicitationRequestFact)
        }
        if set(private_by_key) != expected_private:
            raise ValueError("MCP URL private payload set does not match requests")
        slots: list[McpElicitationItemSlot] = []
        for request in requests:
            if isinstance(request, McpFormElicitationRequestFact):
                slots.append(
                    McpFormItemSlot(
                        request=request,
                        state=McpFormItemState.AWAITING_FORM_RESPONSE,
                        response=None,
                    )
                )
            else:
                private = private_by_key[request.key]
                slots.append(
                    McpUrlItemSlot(
                        request=request,
                        private_url_payload_fingerprint=(
                            private.process_local_private_payload_fingerprint
                        ),
                        state=McpUrlItemState.AWAITING_URL_CONSENT,
                        launch_attempt_generation=0,
                        response=None,
                    )
                )
        self.identity = identity
        self._state = McpElicitationBatchState.COLLECTING
        self._slots = tuple(slots)
        self._private_urls = private_by_key
        self._response_factory = response_factory
        self._browser = browser_port
        if self._browser is not None:
            self._browser.register_owner(
                owner_id=self.identity.owner_id,
                private_url_payloads=private_url_payloads,
            )
        self._frozen_resolution: McpFrozenRoundInputResponses | None = None
        self._physical_tasks: set[asyncio.Task[object]] = set()
        self._lock = RLock()

    @property
    def state(self) -> McpElicitationBatchState:
        with self._lock:
            return self._state

    @property
    def item_slots(self) -> tuple[McpElicitationItemSlot, ...]:
        with self._lock:
            return self._slots

    @property
    def frozen_resolution(self) -> McpFrozenRoundInputResponses | None:
        with self._lock:
            return self._frozen_resolution

    def exact_url_for_display(self, *, request_key: str) -> str:
        with self._lock:
            _index, slot = self._slot(request_key)
            if not isinstance(slot, McpUrlItemSlot):
                raise TypeError("MCP URL display requested for a form item")
            browser = self._browser
            if browser is None:
                raise RuntimeError("MCP URL browser capability is unavailable")
        return browser.exact_url_for_display(
            owner_id=self.identity.owner_id,
            request_key=request_key,
        )

    def submit_form(
        self,
        *,
        request_key: str,
        action: McpElicitationAction,
        content_present: bool,
        content: Mapping[str, object] | None,
    ) -> None:
        with self._lock:
            index, slot = self._slot(request_key)
            if not isinstance(slot, McpFormItemSlot):
                raise TypeError("MCP response mode does not match URL request")
            response = self._response_factory.form_response(
                slot.request,
                action=action,
                content_present=content_present,
                content=content,
            )
            if slot.state is not McpFormItemState.AWAITING_FORM_RESPONSE:
                if (
                    slot.response is not None
                    and slot.response._process_local_response_fingerprint
                    == response._process_local_response_fingerprint
                ):
                    return
                raise RuntimeError("MCP form request has a conflicting terminal response")
            self._replace_slot_and_freeze(
                index,
                replace(
                    slot,
                    state=McpFormItemState.TERMINAL_RESPONSE_FROZEN,
                    response=response,
                ),
            )

    def decline_or_cancel_url(
        self,
        *,
        request_key: str,
        action: McpElicitationAction,
    ) -> None:
        if action not in {McpElicitationAction.DECLINE, McpElicitationAction.CANCEL}:
            raise ValueError("URL terminal response requires decline or cancel")
        with self._lock:
            index, slot = self._slot(request_key)
            if not isinstance(slot, McpUrlItemSlot):
                raise TypeError("MCP response mode does not match form request")
            response = self._response_factory.url_response(
                slot.request,
                action=action,
            )
            if slot.state not in {
                McpUrlItemState.AWAITING_URL_CONSENT,
                McpUrlItemState.AWAITING_URL_RETRY,
            }:
                if (
                    slot.response is not None
                    and slot.response._process_local_response_fingerprint
                    == response._process_local_response_fingerprint
                ):
                    return
                raise RuntimeError("MCP URL request has a conflicting terminal response")
            self._replace_slot_and_freeze(
                index,
                replace(
                    slot,
                    state=McpUrlItemState.TERMINAL_RESPONSE_FROZEN,
                    response=response,
                    sanitized_launch_diagnostic=None,
                ),
            )

    async def launch_url(
        self,
        *,
        request_key: str,
        consent_receipt_fingerprint: str,
    ) -> McpUrlLaunchOutcome:
        with self._lock:
            index, slot = self._slot(request_key)
            if not isinstance(slot, McpUrlItemSlot):
                raise TypeError("MCP URL launch requested for a form item")
            if slot.state is not McpUrlItemState.AWAITING_URL_CONSENT:
                raise RuntimeError("MCP URL item is not awaiting consent")
            if self._browser is None:
                raise RuntimeError("MCP URL browser capability is unavailable")
            generation = slot.launch_attempt_generation + 1
            self._replace_slot(
                index,
                replace(
                    slot,
                    state=McpUrlItemState.LAUNCHING,
                    launch_attempt_generation=generation,
                    sanitized_launch_diagnostic=None,
                ),
            )
            authority = McpConfirmedUrlLaunchAuthority(
                request_key=request_key,
                private_url_payload_fingerprint=slot.private_url_payload_fingerprint,
                consent_receipt_fingerprint=consent_receipt_fingerprint,
                owner_id=self.identity.owner_id,
                owner_generation=generation,
            )
            task = asyncio.create_task(
                self._drive_url_launch(
                    index=index,
                    request_key=request_key,
                    generation=generation,
                    authority=authority,
                ),
                name=f"pulsara-mcp-url-launch:{self.identity.owner_id}:{request_key}",
            )
            self._physical_tasks.add(task)
            task.add_done_callback(self._physical_task_done)
        return await asyncio.shield(task)

    async def _drive_url_launch(
        self,
        *,
        index: int,
        request_key: str,
        generation: int,
        authority: McpConfirmedUrlLaunchAuthority,
    ) -> McpUrlLaunchOutcome:
        browser = self._browser
        if browser is None:
            raise RuntimeError("MCP URL browser capability is unavailable")
        try:
            outcome = await browser.launch(authority)
        except asyncio.CancelledError:
            with self._lock:
                self._state = McpElicitationBatchState.RECONCILIATION_REQUIRED
            raise
        except Exception:
            outcome = McpUrlLaunchOutcome(
                disposition=McpUrlLaunchDisposition.FAILED,
                physical_operation_id=(
                    f"mcp_browser_launch_failed:{self.identity.owner_id}:"
                    f"{request_key}:{generation}"
                ),
                sanitized_diagnostic="The system browser could not be opened.",
            )
        with self._lock:
            current_index, current = self._slot(request_key)
            if (
                current_index != index
                or not isinstance(current, McpUrlItemSlot)
                or current.state is not McpUrlItemState.LAUNCHING
                or current.launch_attempt_generation != generation
            ):
                self._state = McpElicitationBatchState.RECONCILIATION_REQUIRED
                raise RuntimeError("MCP URL launch lost its item owner")
            if outcome.disposition is McpUrlLaunchDisposition.LAUNCHED:
                updated = replace(
                    current,
                    state=McpUrlItemState.AWAITING_URL_RETRY,
                    sanitized_launch_diagnostic=None,
                )
            else:
                updated = replace(
                    current,
                    state=McpUrlItemState.AWAITING_URL_CONSENT,
                    sanitized_launch_diagnostic=outcome.sanitized_diagnostic,
                )
            self._replace_slot(index, updated)
        return outcome

    def _physical_task_done(self, task: asyncio.Task[object]) -> None:
        with self._lock:
            self._physical_tasks.discard(task)
        if task.cancelled():
            return
        task.exception()

    def confirm_url_retry(self, *, request_key: str) -> None:
        with self._lock:
            index, slot = self._slot(request_key)
            if not isinstance(slot, McpUrlItemSlot):
                raise TypeError("MCP URL retry requested for a form item")
            response = self._response_factory.url_response(
                slot.request,
                action=McpElicitationAction.ACCEPT,
            )
            if slot.state is not McpUrlItemState.AWAITING_URL_RETRY:
                if (
                    slot.response is not None
                    and slot.response._process_local_response_fingerprint
                    == response._process_local_response_fingerprint
                ):
                    return
                raise RuntimeError("MCP URL item has not completed a matching launch")
            self._replace_slot_and_freeze(
                index,
                replace(
                    slot,
                    state=McpUrlItemState.TERMINAL_RESPONSE_FROZEN,
                    response=response,
                ),
            )

    def begin_commit(self) -> McpFrozenRoundInputResponses:
        with self._lock:
            if (
                self._state is not McpElicitationBatchState.RESOLUTION_READY
                or self._frozen_resolution is None
            ):
                raise RuntimeError("MCP elicitation batch is not resolution-ready")
            self._state = McpElicitationBatchState.COMMITTING
            return self._frozen_resolution

    def confirm_commit(self, outcome: str) -> None:
        with self._lock:
            if self._state is not McpElicitationBatchState.COMMITTING:
                raise RuntimeError("MCP elicitation batch has no commit in flight")
            if outcome == "full":
                self._state = McpElicitationBatchState.FULL
            elif outcome == "none":
                self._state = McpElicitationBatchState.RESOLUTION_READY
            elif outcome == "unknown":
                self._state = McpElicitationBatchState.RECONCILIATION_REQUIRED
            else:
                self._state = McpElicitationBatchState.ABORTING

    async def drain(self, *, deadline_monotonic: float) -> None:
        while True:
            with self._lock:
                tasks = tuple(self._physical_tasks)
            if not tasks:
                return
            remaining = deadline_monotonic - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("MCP elicitation batch physical tasks did not drain")
            done, _ = await asyncio.wait(tasks, timeout=remaining)
            if not done:
                raise TimeoutError("MCP elicitation batch physical tasks did not drain")
            with self._lock:
                self._physical_tasks.difference_update(done)

    def retire(self) -> None:
        with self._lock:
            if self._physical_tasks:
                raise RuntimeError("MCP elicitation batch still owns physical tasks")
            self._state = McpElicitationBatchState.RETIRED
            browser = self._browser
        if browser is not None:
            browser.release_owner(owner_id=self.identity.owner_id)

    def _slot(self, key: str) -> tuple[int, McpElicitationItemSlot]:
        for index, slot in enumerate(self._slots):
            if slot.request.key == key:
                return index, slot
        raise KeyError(f"unknown MCP input request key: {key}")

    def _replace_slot(self, index: int, slot: McpElicitationItemSlot) -> None:
        values = list(self._slots)
        values[index] = slot
        self._slots = tuple(values)

    def _replace_slot_and_freeze(
        self,
        index: int,
        slot: McpElicitationItemSlot,
    ) -> None:
        previous_slots = self._slots
        previous_state = self._state
        previous_resolution = self._frozen_resolution
        self._replace_slot(index, slot)
        try:
            self._freeze_if_complete()
        except BaseException:
            self._slots = previous_slots
            self._state = previous_state
            self._frozen_resolution = previous_resolution
            raise

    def _freeze_if_complete(self) -> None:
        responses: list[McpElicitationResponse] = []
        for slot in self._slots:
            response = slot.response
            if response is None:
                return
            responses.append(response)
        frozen = self._response_factory.freeze_round(
            request_set_fingerprint=self.identity.request_set_fingerprint,
            ordered_request_keys=self.identity.ordered_request_keys,
            responses=tuple(responses),
        )
        self._frozen_resolution = frozen
        self._state = McpElicitationBatchState.RESOLUTION_READY


def build_mcp_elicitation_batch_owner(
    *,
    runtime_session_id: str,
    interaction_id: str,
    round_ordinal: int,
    request_set_fingerprint: str,
    requests: tuple[McpElicitationRequestFact, ...],
    private_url_payloads: tuple[McpPrivateUrlElicitationPayload, ...],
    response_factory: McpSealedElicitationResponseFactory,
    browser_port: McpExternalBrowserPort | None,
) -> McpElicitationBatchOwner:
    keys = tuple(item.key for item in requests)
    owner_id = context_fingerprint(
        "mcp-elicitation-batch-owner:v1",
        {
            "runtime_session_id": runtime_session_id,
            "interaction_id": interaction_id,
            "round_ordinal": round_ordinal,
            "request_set_fingerprint": request_set_fingerprint,
            "ordered_request_keys": keys,
            "process_nonce": uuid4().hex,
        },
    )
    return McpElicitationBatchOwner(
        identity=McpElicitationBatchIdentity(
            owner_id=owner_id,
            runtime_session_id=runtime_session_id,
            interaction_id=interaction_id,
            round_ordinal=round_ordinal,
            request_set_fingerprint=request_set_fingerprint,
            ordered_request_keys=keys,
            owner_generation=1,
        ),
        requests=requests,
        private_url_payloads=private_url_payloads,
        response_factory=response_factory,
        browser_port=browser_port,
    )


def build_recovered_resolved_mcp_elicitation_batch_owner(
    *,
    runtime_session_id: str,
    interaction_id: str,
    round_ordinal: int,
    request_set_fingerprint: str,
    requests: tuple[McpElicitationRequestFact, ...],
) -> McpRecoveredResolvedBatchOwner:
    """Rebuild only the safe tombstone after a FULL resolution.

    Exact URLs and individual form values deliberately cannot be recovered from
    this owner.  The encrypted replay carrier remains their sole live authority.
    """

    owner_id = f"mcp_elicitation_batch:recovered:{uuid4().hex}"
    identity = McpElicitationBatchIdentity(
        owner_id=owner_id,
        runtime_session_id=runtime_session_id,
        interaction_id=interaction_id,
        round_ordinal=round_ordinal,
        request_set_fingerprint=request_set_fingerprint,
        ordered_request_keys=tuple(item.key for item in requests),
        owner_generation=1,
    )
    slots: list[McpElicitationItemSlot] = []
    for request in requests:
        if isinstance(request, McpFormElicitationRequestFact):
            slots.append(
                McpFormItemSlot(
                    request=request,
                    state=McpFormItemState.TERMINAL_RESPONSE_FROZEN,
                    response=None,
                )
            )
        else:
            slots.append(
                McpUrlItemSlot(
                    request=request,
                    private_url_payload_fingerprint=request.private_url_commitment,
                    state=McpUrlItemState.TERMINAL_RESPONSE_FROZEN,
                    launch_attempt_generation=0,
                    response=None,
                )
            )
    return McpRecoveredResolvedBatchOwner(
        identity=identity,
        item_slots=tuple(slots),
    )


__all__ = [name for name in globals() if name.startswith("Mcp")] + [
    "build_mcp_elicitation_batch_owner",
    "build_recovered_resolved_mcp_elicitation_batch_owner",
]
