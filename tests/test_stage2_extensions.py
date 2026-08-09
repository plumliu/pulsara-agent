from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256

import pytest

from pulsara_agent.conversation_kernel.extensions import (
    ExtensionDelivery,
    ExtensionDeliveryKind,
    ExtensionPlane,
    ExtensionPrincipal,
    ExtensionProjectionProfile,
    ExtensionRegistrationRequest,
    KernelExtensionHost,
    PostCommitHookOffer,
)
from pulsara_agent.conversation_kernel.live import (
    LiveAgentEvent,
    LiveBlockKind,
    LiveChannelKind,
)
from pulsara_agent.ports.live_agent_event import ToolCallDeltaPayload
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType


def _request(
    callback,
    *,
    host: KernelExtensionHost,
    plane: ExtensionPlane = ExtensionPlane.POST_COMMIT,
    event_types: frozenset[str] = frozenset(),
    maximum_queue_events: int = 8,
    maximum_queue_bytes: int = 4096,
    lease_seconds: float = 30,
) -> ExtensionRegistrationRequest:
    return ExtensionRegistrationRequest(
        principal=host.authenticate_principal(
            extension_principal_id="extension:first-party",
        ),
        handler_id="handler:test",
        manifest_digest="sha256:" + "1" * 64,
        plane=plane,
        session_id="session:test",
        turn_id=None,
        event_types=event_types,
        projection_major=1,
        projection_profile=ExtensionProjectionProfile.REDACTED,
        capability_set=frozenset(),
        lease_seconds=lease_seconds,
        maximum_queue_events=maximum_queue_events,
        maximum_queue_bytes=maximum_queue_bytes,
        callback_deadline_seconds=0.1,
        callback=callback,
    )


def _offer(sequence: int, *, payload: str = "accepted") -> PostCommitHookOffer:
    return PostCommitHookOffer(
        event_type=CommittedEventType.ASSISTANT_MESSAGE_ACCEPTED,
        session_id="session:test",
        turn_id="turn:test",
        subject_id=f"entry:{sequence}",
        event_sequence=sequence,
        public_payload={"status": payload},
    )


def test_post_commit_registration_cut_has_no_durable_catchup() -> None:
    async def exercise() -> None:
        deliveries: list[ExtensionDelivery] = []
        delivered = asyncio.Event()

        async def callback(item: ExtensionDelivery) -> None:
            deliveries.append(item)
            delivered.set()

        host = KernelExtensionHost(
            session_id="session:test",
            authenticated_first_party_principal_ids=frozenset(
                {"extension:first-party"}
            ),
        )
        host.offer_post_commit_nowait(_offer(1))
        generation, revision = host.current_cut(ExtensionPlane.POST_COMMIT)
        lease = host.register(
            _request(callback, host=host),
            registration_cut_generation=generation,
            registration_cut_revision=revision,
        )
        host.offer_post_commit_nowait(_offer(1))
        await asyncio.sleep(0)
        assert deliveries == []
        host.offer_post_commit_nowait(_offer(2))
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert [(item.kind, item.source_revision) for item in deliveries] == [
            (ExtensionDeliveryKind.EVENT, 2)
        ]
        assert host.revoke(lease) is True
        await host.aclose(deadline_monotonic=asyncio.get_running_loop().time() + 1)

    asyncio.run(exercise())


def test_hook_overflow_delivers_one_gap_then_detaches_without_gating_offer() -> None:
    async def exercise() -> None:
        deliveries: list[ExtensionDelivery] = []
        delivered = asyncio.Event()

        async def callback(item: ExtensionDelivery) -> None:
            deliveries.append(item)
            delivered.set()

        host = KernelExtensionHost(
            session_id="session:test",
            authenticated_first_party_principal_ids=frozenset(
                {"extension:first-party"}
            ),
        )
        host.register(
            _request(callback, host=host, maximum_queue_events=1),
            registration_cut_generation=1,
            registration_cut_revision=0,
        )
        # No await between offers: the second bounded offer replaces the
        # undelivered event with a GAP.  Canonical callers never wait.
        host.offer_post_commit_nowait(_offer(1))
        host.offer_post_commit_nowait(_offer(2))
        host.offer_post_commit_nowait(_offer(3))
        await asyncio.wait_for(delivered.wait(), timeout=1)
        await asyncio.sleep(0)
        assert len(deliveries) == 1
        assert deliveries[0].kind is ExtensionDeliveryKind.GAP
        assert deliveries[0].omitted_events >= 2
        await host.aclose(deadline_monotonic=asyncio.get_running_loop().time() + 1)

    asyncio.run(exercise())


def test_extension_vocabulary_and_sensitive_profiles_are_closed() -> None:
    async def callback(_item: ExtensionDelivery) -> None:
        return None

    host = KernelExtensionHost(
        session_id="session:test",
        authenticated_first_party_principal_ids=frozenset(
            {"extension:first-party"}
        ),
    )
    base = _request(callback, host=host)
    with pytest.raises(PermissionError, match="authenticated by Host"):
        host.register(
            replace(
                base,
                principal=ExtensionPrincipal(
                    "extension:forged", True, object()
                ),
            ),
            registration_cut_generation=1,
            registration_cut_revision=0,
        )
    with pytest.raises(ValueError, match="foreign event type"):
        host.register(
            replace(base, event_types=frozenset({"CustomEvent"})),
            registration_cut_generation=1,
            registration_cut_revision=0,
        )
    with pytest.raises(PermissionError, match="first-party"):
        host.register(
            replace(
                base,
                principal=host.authenticate_principal(
                    extension_principal_id="extension:third-party",
                ),
                projection_profile=(
                    ExtensionProjectionProfile.UNREDACTED_TOOL_ARGUMENTS
                ),
                capability_set=frozenset({"unredacted_tool_arguments"}),
            ),
            registration_cut_generation=1,
            registration_cut_revision=0,
        )


def test_expired_extension_lease_drops_future_offers() -> None:
    async def exercise() -> None:
        deliveries: list[ExtensionDelivery] = []

        async def callback(item: ExtensionDelivery) -> None:
            deliveries.append(item)

        host = KernelExtensionHost(
            session_id="session:test",
            authenticated_first_party_principal_ids=frozenset(
                {"extension:first-party"}
            ),
        )
        host.register(
            _request(callback, host=host, lease_seconds=0.01),
            registration_cut_generation=1,
            registration_cut_revision=0,
        )
        await asyncio.sleep(0.03)
        host.offer_post_commit_nowait(_offer(1))
        await asyncio.sleep(0)
        assert deliveries == []
        await host.aclose(deadline_monotonic=asyncio.get_running_loop().time() + 1)

    asyncio.run(exercise())


def test_redacted_live_extension_never_receives_tool_argument_text() -> None:
    async def exercise() -> None:
        deliveries: list[ExtensionDelivery] = []
        delivered = asyncio.Event()

        async def callback(item: ExtensionDelivery) -> None:
            deliveries.append(item)
            delivered.set()

        host = KernelExtensionHost(session_id="session:test")
        principal = host.authenticate_principal(
            extension_principal_id="extension:third-party"
        )
        host.register(
            replace(
                _request(callback, host=host, plane=ExtensionPlane.LIVE),
                principal=principal,
                event_types=frozenset({LiveEventType.TOOL_CALL_DELTA.value}),
            ),
            registration_cut_generation=1,
            registration_cut_revision=0,
        )
        secret = '{"token":"secret"}'
        host.offer_live_nowait(
            LiveAgentEvent(
                generation=1,
                revision=1,
                event_type=LiveEventType.TOOL_CALL_DELTA,
                session_id="session:test",
                turn_id="turn:test",
                draft_identity="entry:test",
                payload=ToolCallDeltaPayload(
                    block_identity="block:test",
                    tool_call_id="call:test",
                    delta=secret,
                ),
                scope_kind="ROOT",
                scope_subagent_task_id=None,
                channel_kind=LiveChannelKind.MODEL_OUTPUT,
                channel_tool_call_id=None,
                channel_attempt_id=None,
                generation_id="model-output:entry:test",
                proposed_entry_id="entry:test",
                block_id="block:test",
                block_ordinal=0,
                block_kind=LiveBlockKind.TOOL_CALL,
            )
        )
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert len(deliveries) == 1
        encoded = repr(deliveries[0].payload)
        assert "secret" not in encoded
        assert deliveries[0].payload["payload"] == {
            "block_identity": "block:test",
            "tool_call_id": "call:test",
            "delta_redacted": True,
            "delta_utf8_bytes": len(secret.encode("utf-8")),
            "delta_digest": "sha256:"
            + sha256(secret.encode("utf-8")).hexdigest(),
        }
        await host.aclose(deadline_monotonic=asyncio.get_running_loop().time() + 1)

    asyncio.run(exercise())
