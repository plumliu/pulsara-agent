"""One-shot physical authorities for provider dispatch preparation."""

from __future__ import annotations

from threading import Lock
from pulsara_agent.llm.frozen_target import FrozenEpochModelCallTarget
from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
)
from pulsara_agent.conversation_kernel.reader import (
    OwnerIssuedCanonicalDispatchObservation,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    OwnerIssuedCapabilityDispatchObservation,
)
from pulsara_agent.conversation_kernel.safe_point import (
    PreparedProviderInputHandle,
    ProviderSafePointCoordinator,
    SealedProviderInputPreparationBasis,
)
from pulsara_agent.model_input.contracts import PreparedProviderInputCut
from pulsara_agent.model_input.continuity import FrozenProviderInputAppendPlanningInput
from pulsara_agent.conversation_kernel.tool_surface import ProcessLocalToolSurfaceBorrow


class ProviderDispatchExecutionAuthority:
    """One-shot owner of the physical resources for one provider dispatch."""

    __slots__ = (
        "_basis",
        "_handle",
        "_installable",
        "_surface_borrow",
        "_safe_point",
        "_lock",
    )

    def __init__(
        self,
        handle: PreparedProviderInputHandle,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
        *,
        safe_point: ProviderSafePointCoordinator,
        canonical_observation: OwnerIssuedCanonicalDispatchObservation,
        capability_observation: OwnerIssuedCapabilityDispatchObservation,
        call_target: FrozenEpochModelCallTarget,
        installable: bool = True,
    ) -> None:
        if handle is None or surface_borrow is None:
            raise ValueError("provider execution authority is incomplete")
        self._handle: PreparedProviderInputHandle | None = handle
        self._surface_borrow: ProcessLocalToolSurfaceBorrow | None = surface_borrow
        self._safe_point = safe_point
        self._installable = installable
        self._basis = safe_point.seal_provider_input_preparation_basis(
            handle=handle,
            canonical_observation=canonical_observation,
            capability_observation=capability_observation,
            call_target=call_target,
        )
        self._lock = Lock()

    @property
    def basis(self) -> SealedProviderInputPreparationBasis:
        return self._basis

    @property
    def cut(self) -> PreparedProviderInputCut:
        with self._lock:
            handle = self._handle
        if handle is None:
            raise RuntimeError("provider execution authority is already consumed")
        return handle.cut

    def require_for_install(
        self,
    ) -> tuple[
        SealedProviderInputPreparationBasis,
        PreparedProviderInputHandle,
        ProcessLocalToolSurfaceBorrow,
    ]:
        with self._lock:
            handle = self._handle
            borrow = self._surface_borrow
            installable = self._installable
        if handle is None or borrow is None or not installable:
            raise RuntimeError("provider execution authority is already consumed")
        return self._basis, handle, borrow

    def close(self) -> None:
        with self._lock:
            handle = self._handle
            borrow = self._surface_borrow
            self._handle = None
            self._surface_borrow = None
        if handle is None or borrow is None:
            raise RuntimeError("provider execution authority is already consumed")
        try:
            self._safe_point.revoke_provider_input_preparation_basis(self._basis)
            handle.close()
        finally:
            borrow.close()

    def take_handle_for_rotation(self) -> PreparedProviderInputHandle:
        """Consume the authority while retiring the obsolete Tool borrow."""

        with self._lock:
            handle = self._handle
            borrow = self._surface_borrow
            self._handle = None
            self._surface_borrow = None
        if handle is None or borrow is None:
            raise RuntimeError("provider execution authority is already consumed")
        try:
            try:
                self._safe_point.revoke_provider_input_preparation_basis(self._basis)
            finally:
                borrow.close()
        except BaseException:
            handle.close()
            raise
        return handle

    def take_for_suffix_preparation(
        self,
    ) -> tuple[PreparedProviderInputHandle, ProcessLocalToolSurfaceBorrow]:
        """Transfer the pinned surface and cut without issuing a transition."""
        with self._lock:
            handle = self._handle
            borrow = self._surface_borrow
            self._handle = None
            self._surface_borrow = None
        if handle is None or borrow is None:
            raise RuntimeError("provider execution authority is already consumed")
        self._safe_point.revoke_provider_input_preparation_basis(self._basis)
        return handle, borrow

    def finish_model_operation(self) -> ProcessLocalToolSurfaceBorrow:
        """Close the safe-point handle and transfer the Tool borrow to execution."""

        with self._lock:
            handle = self._handle
            borrow = self._surface_borrow
            self._handle = None
            self._surface_borrow = None
        if handle is None or borrow is None:
            raise RuntimeError("provider execution authority is already consumed")
        try:
            self._safe_point.revoke_provider_input_preparation_basis(self._basis)
            handle.close()
        except BaseException:
            borrow.close()
            raise
        return borrow


class CompactionDryResourceAuthority:
    """Own resources for one pre-adoption dry projection, never installation."""

    __slots__ = (
        "_basis",
        "_handle",
        "_continuity",
        "_planning",
        "_abort_planning",
        "_surface_borrow",
        "_safe_point",
        "_lock",
    )

    def __init__(
        self,
        handle: PreparedProviderInputHandle,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
        *,
        safe_point: ProviderSafePointCoordinator,
        canonical_observation: OwnerIssuedCanonicalDispatchObservation,
        capability_observation: OwnerIssuedCapabilityDispatchObservation,
        call_target: FrozenEpochModelCallTarget,
        continuity: HostProviderInputContinuityOwner,
        planning: FrozenProviderInputAppendPlanningInput,
    ) -> None:
        self._handle: PreparedProviderInputHandle | None = handle
        self._continuity = continuity
        self._planning = planning
        self._abort_planning = True
        self._surface_borrow: ProcessLocalToolSurfaceBorrow | None = surface_borrow
        self._safe_point = safe_point
        self._basis = safe_point.seal_provider_input_preparation_basis(
            handle=handle,
            canonical_observation=canonical_observation,
            capability_observation=capability_observation,
            call_target=call_target,
        )
        self._lock = Lock()

    def begin_empty_adoption(self) -> None:
        with self._lock:
            if self._handle is None or not self._abort_planning:
                raise RuntimeError("compaction dry resources are already consumed")
            self._abort_planning = False

    def restore_empty_adoption_abort(self) -> None:
        with self._lock:
            if self._handle is None or self._abort_planning:
                raise RuntimeError("compaction dry resources cannot be restored")
            self._abort_planning = True

    def _abort_source_planning_if_owned(self) -> None:
        with self._lock:
            abort = self._abort_planning
            self._abort_planning = False
        if abort:
            self._continuity.abort_planning(self._planning)

    def require_for_empty_adoption(
        self,
    ) -> tuple[SealedProviderInputPreparationBasis, PreparedProviderInputHandle]:
        with self._lock:
            handle = self._handle
            borrow = self._surface_borrow
        if handle is None or borrow is None:
            raise RuntimeError("compaction dry resources are already consumed")
        return self._basis, handle

    def close(self) -> None:
        with self._lock:
            handle = self._handle
            borrow = self._surface_borrow
            self._handle = None
            self._surface_borrow = None
        if handle is None or borrow is None:
            raise RuntimeError("compaction dry resources are already consumed")
        try:
            self._safe_point.revoke_provider_input_preparation_basis(self._basis)
            handle.close()
        finally:
            try:
                borrow.close()
            finally:
                self._abort_source_planning_if_owned()

    def take_handle_for_rotation(self) -> PreparedProviderInputHandle:
        with self._lock:
            handle = self._handle
            borrow = self._surface_borrow
            self._handle = None
            self._surface_borrow = None
        if handle is None or borrow is None:
            raise RuntimeError("compaction dry resources are already consumed")
        self._safe_point.revoke_provider_input_preparation_basis(self._basis)
        try:
            borrow.close()
        finally:
            self._abort_source_planning_if_owned()
        return handle
