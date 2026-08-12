"""Typed, bounded, process-local Stage 2 extension protocol."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
from threading import RLock
from time import monotonic
from typing import Awaitable, Callable, Mapping
from uuid import uuid4

from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.live import LiveAgentEvent
from pulsara_agent.ports.live_agent_event import payload_to_mapping
from pulsara_agent.conversation_kernel.vocabulary import (
    CommittedEventType,
    LIVE_EVENT_TYPES,
)


class ExtensionPlane(StrEnum):
    LIVE = "LIVE"
    POST_COMMIT = "POST_COMMIT"
    OPERATIONAL = "OPERATIONAL"


class ExtensionProjectionProfile(StrEnum):
    REDACTED = "REDACTED"
    RAW_THINKING = "RAW_THINKING"
    UNREDACTED_TOOL_ARGUMENTS = "UNREDACTED_TOOL_ARGUMENTS"


class ExtensionDeliveryKind(StrEnum):
    EVENT = "EVENT"
    GAP = "GAP"


class OperationalHookType(StrEnum):
    MODEL_INPUT_COMPILE_OBSERVED = "ModelInputCompileObserved"
    PROVIDER_USAGE_OBSERVED = "ProviderUsageObserved"
    FOREGROUND_TURN_FAILED = "ForegroundTurnFailed"
    PROVIDER_CONTINUITY_FAILED = "ProviderContinuityFailed"
    BLOB_ORPHAN_GC_FAILED = "BlobOrphanGcFailed"


@dataclass(frozen=True, slots=True)
class ExtensionPrincipal:
    extension_principal_id: str
    authenticated_first_party: bool
    _host_authority: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ExtensionRegistrationRequest:
    principal: ExtensionPrincipal
    handler_id: str
    manifest_digest: str
    plane: ExtensionPlane
    session_id: str
    turn_id: str | None
    event_types: frozenset[str]
    projection_major: int
    projection_profile: ExtensionProjectionProfile
    capability_set: frozenset[str]
    lease_seconds: float
    maximum_queue_events: int
    maximum_queue_bytes: int
    callback_deadline_seconds: float
    callback: Callable[["ExtensionDelivery"], Awaitable[None]] = field(
        repr=False, compare=False
    )


@dataclass(frozen=True, slots=True)
class ExtensionRegistrationLease:
    registration_id: str
    lease_generation: int
    expires_at_monotonic: float
    registration_cut_generation: int
    registration_cut_revision: int


@dataclass(frozen=True, slots=True)
class PostCommitHookOffer:
    event_type: CommittedEventType
    session_id: str
    turn_id: str | None
    subject_id: str
    event_sequence: int
    public_payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class OperationalHookOffer:
    event_type: OperationalHookType
    session_id: str
    turn_id: str | None
    public_payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ExtensionDelivery:
    kind: ExtensionDeliveryKind
    registration_id: str
    plane: ExtensionPlane
    source_generation: int
    source_revision: int
    event_type: str | None
    payload: Mapping[str, object]
    omitted_events: int = 0


@dataclass(slots=True)
class _Registration:
    request: ExtensionRegistrationRequest
    lease: ExtensionRegistrationLease
    queue: deque[tuple[ExtensionDelivery, int]]
    queue_bytes: int
    wake: asyncio.Event
    worker: asyncio.Task[None] | None
    revoked: bool = False
    detach_after_delivery: bool = False


class KernelExtensionHost:
    """Best-effort hook delivery with no canonical mutation capability."""

    def __init__(
        self,
        *,
        session_id: str,
        authenticated_first_party_principal_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._session_id = session_id
        self._registrations: dict[str, _Registration] = {}
        self._lock = RLock()
        self._accepting = True
        self._post_commit_revision = 0
        self._operational_revision = 0
        self._principal_authority = object()
        self._authenticated_first_party_principal_ids = (
            authenticated_first_party_principal_ids
        )

    def authenticate_principal(
        self, *, extension_principal_id: str
    ) -> ExtensionPrincipal:
        """Mint one process-local principal after Host authentication.

        The returned carrier is scoped to this Host extension owner.  A
        registration caller cannot grant itself first-party projection access
        by constructing a DTO with a boolean field.
        """

        if not extension_principal_id:
            raise ValueError("extension principal identity is empty")
        return ExtensionPrincipal(
            extension_principal_id=extension_principal_id,
            authenticated_first_party=(
                extension_principal_id in self._authenticated_first_party_principal_ids
            ),
            _host_authority=self._principal_authority,
        )

    def current_cut(self, plane: ExtensionPlane) -> tuple[int, int]:
        """Return the process-local tap cut, never a durable journal cursor."""

        with self._lock:
            if plane is ExtensionPlane.POST_COMMIT:
                return 1, self._post_commit_revision
            if plane is ExtensionPlane.OPERATIONAL:
                return 1, self._operational_revision
        raise ValueError("live registration cuts are owned by LiveAgentEventBus")

    def register(
        self,
        request: ExtensionRegistrationRequest,
        *,
        registration_cut_generation: int,
        registration_cut_revision: int,
    ) -> ExtensionRegistrationLease:
        self._validate_request(request)
        with self._lock:
            if not self._accepting:
                raise RuntimeError("extension registration is closed")
            active_registrations = sum(
                not item.revoked and monotonic() < item.lease.expires_at_monotonic
                for item in self._registrations.values()
            )
            if active_registrations >= STAGE2_LIMITS.live_observer_hard_count:
                raise RuntimeError("extension registration capacity is exhausted")
            registration_id = f"extension-registration:{uuid4().hex}"
            lease = ExtensionRegistrationLease(
                registration_id=registration_id,
                lease_generation=1,
                expires_at_monotonic=monotonic() + request.lease_seconds,
                registration_cut_generation=registration_cut_generation,
                registration_cut_revision=registration_cut_revision,
            )
            wake = asyncio.Event()
            registration = _Registration(
                request=request,
                lease=lease,
                queue=deque(),
                queue_bytes=0,
                wake=wake,
                worker=None,
            )
            worker = asyncio.create_task(
                self._worker(registration),
                name=f"kernel-extension:{registration_id}",
            )
            registration.worker = worker
            self._registrations[registration_id] = registration
            worker.add_done_callback(
                lambda task, identity=registration_id, item=registration: (
                    self._retire_registration(identity, item, task)
                )
            )
            return lease

    def revoke(self, lease: ExtensionRegistrationLease) -> bool:
        with self._lock:
            registration = self._registrations.get(lease.registration_id)
            if registration is None or registration.lease != lease:
                return False
            registration.revoked = True
            registration.queue.clear()
            registration.queue_bytes = 0
            registration.wake.set()
            return True

    def offer_live_nowait(self, event: LiveAgentEvent) -> None:
        payload = {
            "draft_identity": event.draft_identity,
            "block_id": event.block_id,
            "block_ordinal": event.block_ordinal,
            "block_kind": event.block_kind.value,
            "channel_kind": event.channel_kind.value,
            "payload": dict(payload_to_mapping(event.payload)),
        }
        self._offer(
            plane=ExtensionPlane.LIVE,
            event_type=event.event_type.value,
            session_id=event.session_id,
            turn_id=event.turn_id,
            source_generation=event.generation,
            source_revision=event.revision,
            payload=payload,
        )

    def offer_post_commit_nowait(self, offer: PostCommitHookOffer) -> None:
        with self._lock:
            if offer.event_sequence <= self._post_commit_revision:
                return
            self._post_commit_revision = offer.event_sequence
        self._offer(
            plane=ExtensionPlane.POST_COMMIT,
            event_type=offer.event_type.value,
            session_id=offer.session_id,
            turn_id=offer.turn_id,
            source_generation=1,
            source_revision=offer.event_sequence,
            payload={
                "subject_id": offer.subject_id,
                "event_sequence": offer.event_sequence,
                **dict(offer.public_payload),
            },
        )

    def offer_operational_nowait(self, offer: OperationalHookOffer) -> None:
        with self._lock:
            self._operational_revision += 1
            revision = self._operational_revision
        self._offer(
            plane=ExtensionPlane.OPERATIONAL,
            event_type=offer.event_type.value,
            session_id=offer.session_id,
            turn_id=offer.turn_id,
            source_generation=1,
            source_revision=revision,
            payload=dict(offer.public_payload),
        )

    def stop_admission(self) -> None:
        with self._lock:
            self._accepting = False

    async def aclose(self, *, deadline_monotonic: float) -> None:
        self.stop_admission()
        with self._lock:
            registrations = tuple(self._registrations.values())
            for item in registrations:
                item.revoked = True
                item.queue.clear()
                item.queue_bytes = 0
                item.wake.set()
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0 and registrations:
            raise TimeoutError("extension close deadline expired")
        if registrations:
            done, pending = await asyncio.wait(
                tuple(item.worker for item in registrations if item.worker is not None),
                timeout=remaining,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        with self._lock:
            self._registrations.clear()

    def _offer(
        self,
        *,
        plane: ExtensionPlane,
        event_type: str,
        session_id: str,
        turn_id: str | None,
        source_generation: int,
        source_revision: int,
        payload: Mapping[str, object],
    ) -> None:
        if session_id != self._session_id:
            return
        with self._lock:
            if not self._accepting:
                return
            registrations = tuple(self._registrations.values())
            for registration in registrations:
                request = registration.request
                if (
                    registration.revoked
                    or monotonic() >= registration.lease.expires_at_monotonic
                    or request.plane is not plane
                    or (request.turn_id is not None and request.turn_id != turn_id)
                    or (request.event_types and event_type not in request.event_types)
                    or source_generation
                    < registration.lease.registration_cut_generation
                    or (
                        source_generation
                        == registration.lease.registration_cut_generation
                        and source_revision
                        <= registration.lease.registration_cut_revision
                    )
                ):
                    continue
                projected = _project_payload(request, event_type, payload)
                encoded = json.dumps(
                    projected,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                delivery = ExtensionDelivery(
                    kind=ExtensionDeliveryKind.EVENT,
                    registration_id=registration.lease.registration_id,
                    plane=plane,
                    source_generation=source_generation,
                    source_revision=source_revision,
                    event_type=event_type,
                    payload=projected,
                )
                if (
                    len(encoded) > request.maximum_queue_bytes
                    or len(registration.queue) >= request.maximum_queue_events
                    or registration.queue_bytes + len(encoded)
                    > request.maximum_queue_bytes
                ):
                    omitted = len(registration.queue) + 1
                    registration.queue.clear()
                    registration.queue_bytes = 0
                    gap = ExtensionDelivery(
                        kind=ExtensionDeliveryKind.GAP,
                        registration_id=registration.lease.registration_id,
                        plane=plane,
                        source_generation=source_generation,
                        source_revision=source_revision,
                        event_type=None,
                        payload={},
                        omitted_events=omitted,
                    )
                    registration.queue.append((gap, 0))
                    registration.detach_after_delivery = True
                else:
                    registration.queue.append((delivery, len(encoded)))
                    registration.queue_bytes += len(encoded)
                registration.wake.set()

    async def _worker(self, registration: _Registration) -> None:
        while True:
            remaining = registration.lease.expires_at_monotonic - monotonic()
            if remaining <= 0:
                with self._lock:
                    registration.revoked = True
                    registration.queue.clear()
                    registration.queue_bytes = 0
                return
            try:
                await asyncio.wait_for(registration.wake.wait(), timeout=remaining)
            except TimeoutError:
                with self._lock:
                    registration.revoked = True
                    registration.queue.clear()
                    registration.queue_bytes = 0
                return
            registration.wake.clear()
            while True:
                with self._lock:
                    if (
                        registration.revoked
                        or monotonic() >= registration.lease.expires_at_monotonic
                    ):
                        registration.revoked = True
                        registration.queue.clear()
                        registration.queue_bytes = 0
                        return
                    if not registration.queue:
                        break
                    delivery, charge = registration.queue.popleft()
                    registration.queue_bytes -= charge
                if monotonic() >= registration.lease.expires_at_monotonic:
                    with self._lock:
                        registration.revoked = True
                        registration.queue.clear()
                        registration.queue_bytes = 0
                    return
                try:
                    await asyncio.wait_for(
                        registration.request.callback(delivery),
                        timeout=registration.request.callback_deadline_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    # Hook failure is operational-only and never gates products.
                    pass
                with self._lock:
                    if registration.detach_after_delivery:
                        registration.revoked = True
                        registration.queue.clear()
                        registration.queue_bytes = 0
                        return

    def _retire_registration(
        self,
        registration_id: str,
        registration: _Registration,
        task: asyncio.Task[None],
    ) -> None:
        if not task.cancelled():
            task.exception()
        with self._lock:
            if self._registrations.get(registration_id) is registration:
                self._registrations.pop(registration_id, None)

    def _validate_request(self, request: ExtensionRegistrationRequest) -> None:
        if request.session_id != self._session_id:
            raise ValueError("extension session scope is not exact")
        if not all(
            (
                request.principal.extension_principal_id,
                request.handler_id,
                request.manifest_digest,
            )
        ):
            raise ValueError("extension registration identity is incomplete")
        if request.principal._host_authority is not self._principal_authority:
            raise PermissionError("extension principal was not authenticated by Host")
        if request.projection_major != 1:
            raise ValueError("extension projection major is unsupported")
        allowed_types = {
            ExtensionPlane.LIVE: frozenset(LIVE_EVENT_TYPES),
            ExtensionPlane.POST_COMMIT: frozenset(
                item.value for item in CommittedEventType
            ),
            ExtensionPlane.OPERATIONAL: frozenset(
                item.value for item in OperationalHookType
            ),
        }[request.plane]
        if not request.event_types <= allowed_types:
            raise ValueError("extension subscription contains a foreign event type")
        if not 0 < request.callback_deadline_seconds <= 5.0:
            raise ValueError("extension callback deadline exceeds hard cap")
        if not 0 < request.lease_seconds <= 24 * 60 * 60:
            raise ValueError("extension lease duration exceeds hard cap")
        if not 1 <= request.maximum_queue_events <= STAGE2_LIMITS.live_ring_hard_events:
            raise ValueError("extension event queue bound is invalid")
        if not 1 <= request.maximum_queue_bytes <= STAGE2_LIMITS.live_ring_hard_bytes:
            raise ValueError("extension byte queue bound is invalid")
        if request.projection_profile is ExtensionProjectionProfile.RAW_THINKING and (
            not request.principal.authenticated_first_party
            or "raw_thinking" not in request.capability_set
        ):
            raise PermissionError("raw thinking requires a first-party capability")
        if (
            request.projection_profile
            is ExtensionProjectionProfile.UNREDACTED_TOOL_ARGUMENTS
            and (
                not request.principal.authenticated_first_party
                or "unredacted_tool_arguments" not in request.capability_set
            )
        ):
            raise PermissionError(
                "tool arguments require a first-party explicit capability"
            )


def _project_payload(
    request: ExtensionRegistrationRequest,
    event_type: str,
    payload: Mapping[str, object],
) -> Mapping[str, object]:
    value = json.loads(
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    body = value.get("payload")
    if isinstance(body, dict):
        if event_type.startswith("Thinking") and (
            request.projection_profile is not ExtensionProjectionProfile.RAW_THINKING
        ):
            text = str(body.get("delta") or body.get("text") or "")
            body.clear()
            body.update(
                {
                    "redacted": True,
                    "utf8_bytes": len(text.encode("utf-8")),
                    "digest": "sha256:" + sha256(text.encode("utf-8")).hexdigest(),
                }
            )
        if event_type.startswith("ToolCall") and (
            request.projection_profile
            is not ExtensionProjectionProfile.UNREDACTED_TOOL_ARGUMENTS
        ):
            for key in ("arguments", "arguments_json", "delta"):
                if key in body:
                    text = str(body[key])
                    encoded = text.encode("utf-8")
                    del body[key]
                    body[f"{key}_redacted"] = True
                    body[f"{key}_utf8_bytes"] = len(encoded)
                    body[f"{key}_digest"] = "sha256:" + sha256(encoded).hexdigest()
        if event_type == CommittedEventType.TOOL_REMOTE_IDENTITY_PUBLISHED.value:
            raw = body.pop("remote_identity", None)
            if raw is not None:
                encoded = str(raw).encode("utf-8")
                body["remote_identity_utf8_bytes"] = len(encoded)
                body["remote_identity_digest"] = "sha256:" + sha256(encoded).hexdigest()
    return value


__all__ = [
    "ExtensionDelivery",
    "ExtensionDeliveryKind",
    "ExtensionPlane",
    "ExtensionPrincipal",
    "ExtensionProjectionProfile",
    "ExtensionRegistrationLease",
    "ExtensionRegistrationRequest",
    "KernelExtensionHost",
    "OperationalHookOffer",
    "OperationalHookType",
    "PostCommitHookOffer",
]
