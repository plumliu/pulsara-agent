from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.repository import (
    AcceptedEntry,
    ConversationKernelConflict,
)
from pulsara_agent.model_input.contracts import PreparedProviderInputCut
from pulsara_agent.conversation_kernel.safe_point import (
    ExternalSourceNotAtSafePoint,
    ProviderSafePointCoordinator,
)
from pulsara_agent.ports.terminal_observation import (
    NewTurnInstallation,
    PreparedInstallationTarget,
    TerminalDeliveryCoverage,
    TerminalObservationContentV1,
    TerminalObservationInstallationAttempt,
    TerminalObservationKind,
)


def _content() -> TerminalObservationContentV1:
    output = "safe-point output"
    size = len(output.encode())
    return TerminalObservationContentV1(
        observation_id="observation:safe-point",
        monitor_id="monitor:safe-point",
        process_id="process:safe-point",
        observation_ordinal=1,
        observation_kind=TerminalObservationKind.PROGRESS,
        process_status="running",
        exit_code=None,
        output_disposition="EXACT_DELTA",
        gap_before_output=False,
        delivery_coverage=TerminalDeliveryCoverage.COMPLETE,
        available_source_utf8_bytes=size,
        included_source_utf8_bytes=size,
        omitted_by_delivery_bound_utf8_bytes=0,
        output=output,
    )


class _Repository:
    def __init__(self) -> None:
        self.safe_checks = 0
        self.accept_calls = 0
        self.confirm_calls = 0
        self.accept_error: BaseException | None = None
        self.confirmed: AcceptedEntry | None = None

    def require_provider_safe_turn(self, *_args, **_kwargs) -> None:
        self.safe_checks += 1

    def prepare_provider_input_cut(self, guard, *, turn_id, **_kwargs):
        return PreparedProviderInputCut(guard.session_id, turn_id, "revision:0", 1)

    def accept_terminal_observation(self, _guard, *, candidate, **_kwargs):
        self.accept_calls += 1
        if self.accept_error is not None:
            raise self.accept_error
        return AcceptedEntry(
            entry_id=candidate.target.initial_entry_id,
            turn_id=candidate.target.turn_id,
            entry_sequence=2,
            event_sequence=2,
        )

    def confirm_terminal_observation_winner(self, *_args, **_kwargs):
        self.confirm_calls += 1
        return self.confirmed


class _Coordinator:
    def __init__(self) -> None:
        self.attempt: TerminalObservationInstallationAttempt | None = None
        self.freeze_targets: list[PreparedInstallationTarget] = []
        self.settlements: list[tuple[TerminalObservationInstallationAttempt, bool]] = []

    def current_installation_attempt(self, _monitor_id):
        return self.attempt

    def freeze(
        self,
        *,
        monitor_id,
        target,
        workspace_id,
        writer_generation,
        actor_id,
    ):
        self.freeze_targets.append(target)
        content = _content()
        digest = f"sha256:{sha256(content.canonical_bytes()).hexdigest()}"
        self.attempt = TerminalObservationInstallationAttempt(
            session_id="session:safe-point",
            workspace_id=workspace_id,
            writer_generation=writer_generation,
            content=content,
            content_digest=digest,
            retained_from_cursor="cursor:retained",
            through_cursor="cursor:through",
            target=target,
            occurred_at=datetime.now(timezone.utc),
            actor_id=actor_id,
            candidate_fingerprint="sha256:" + "a" * 64,
        )
        assert monitor_id == content.monitor_id
        return self.attempt

    def settle_installation(self, attempt, *, accepted):
        assert attempt == self.attempt
        self.settlements.append((attempt, accepted))
        if accepted or not accepted:
            self.attempt = None


def _safe_point(repository: _Repository) -> ProviderSafePointCoordinator:
    return ProviderSafePointCoordinator(
        repository=repository,  # type: ignore[arg-type]
        guard=HostWriterGuard("session:safe-point", 1, "host:safe-point"),
    )


def _target(suffix: str) -> NewTurnInstallation:
    return NewTurnInstallation(
        f"turn:{suffix}", f"revision:{suffix}", f"entry:{suffix}"
    )


def test_round2_active_provider_handle_prevents_monitor_freeze() -> None:
    repository = _Repository()
    safe_point = _safe_point(repository)
    coordinator = _Coordinator()
    handle = safe_point.freeze_provider_input(
        turn_id="turn:active", deadline_monotonic=monotonic() + 5
    )
    with pytest.raises(ExternalSourceNotAtSafePoint):
        safe_point.install_terminal_observation(
            coordinator=coordinator,  # type: ignore[arg-type]
            monitor_id="monitor:safe-point",
            target=_target("blocked"),
            workspace_id="workspace:safe-point",
            actor_id="host:safe-point",
            deadline_monotonic=monotonic() + 5,
        )
    assert coordinator.freeze_targets == []
    assert repository.accept_calls == 0
    handle.close()


def test_round2_canonical_safe_predicate_conflict_retires_mutable_candidate() -> None:
    repository = _Repository()
    repository.accept_error = ConversationKernelConflict("tool request is not terminal")
    safe_point = _safe_point(repository)
    coordinator = _Coordinator()
    with pytest.raises(ConversationKernelConflict, match="tool request"):
        safe_point.install_terminal_observation(
            coordinator=coordinator,  # type: ignore[arg-type]
            monitor_id="monitor:safe-point",
            target=_target("conflict"),
            workspace_id="workspace:safe-point",
            actor_id="host:safe-point",
            deadline_monotonic=monotonic() + 5,
        )
    assert repository.accept_calls == 1
    assert len(coordinator.settlements) == 1
    assert coordinator.settlements[0][1] is False


def test_round2_ack_unknown_exact_confirms_original_attempt_without_retarget() -> None:
    repository = _Repository()
    repository.accept_error = TimeoutError("ACK unknown")
    safe_point = _safe_point(repository)
    coordinator = _Coordinator()
    original = _target("original")
    with pytest.raises(TimeoutError, match="ACK unknown"):
        safe_point.install_terminal_observation(
            coordinator=coordinator,  # type: ignore[arg-type]
            monitor_id="monitor:safe-point",
            target=original,
            workspace_id="workspace:safe-point",
            actor_id="host:safe-point",
            deadline_monotonic=monotonic() + 5,
        )
    assert coordinator.attempt is not None
    assert coordinator.attempt.target == original
    assert repository.confirm_calls == 1

    repository.confirmed = AcceptedEntry(
        original.initial_entry_id, original.turn_id, 2, 2
    )
    accepted = safe_point.install_terminal_observation(
        coordinator=coordinator,  # type: ignore[arg-type]
        monitor_id="monitor:safe-point",
        target=_target("must-not-replace-original"),
        workspace_id="workspace:safe-point",
        actor_id="host:safe-point",
        deadline_monotonic=monotonic() + 5,
    )
    assert accepted == repository.confirmed
    assert coordinator.freeze_targets == [original]
    assert repository.accept_calls == 1
    assert repository.confirm_calls == 2
    assert coordinator.settlements[-1][1] is True
