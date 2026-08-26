"""Read-only Pulsara-package Skill definition binding and producer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
import os
from pathlib import Path
import stat

from pulsara_agent.capability.bundled_inventory import (
    BundledInventoryClassification,
    BundledInventoryEntry,
    EXPECTED_BUNDLED_SKILL_NAMES,
    classify_bundled_skill_inventory,
)

from pulsara_agent.local_source_binding import (
    open_absolute_directory_nofollow,
    prepare_local_source_path,
)
from pulsara_agent.capability.local_skills import (
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_LOCATION_BYTES,
    SKILL_FILE_NAME,
    SkillObservationCancellationPort,
    SkillObservationError,
    check_skill_deadline,
    close_skill_roots,
    diagnostic_at,
    enrich_skill_document,
    observe_skill_root,
    parse_skill_document,
    read_observed_skill_document,
    revalidate_skill_roots,
    skill_diagnostic,
    validate_skill_candidate_placement,
)
from pulsara_agent.capability.types import (
    BundledSkillOrigin,
    ProducerUnavailableCause,
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillDiagnosticSeverity,
    SkillManifest,
    SkillProducerKind,
    SkillProducerUnavailableReason,
)

class BundledSkillDefinitionsDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class FrozenBundledSkillDefinitions:
    disposition: BundledSkillDefinitionsDisposition
    candidates: tuple[SkillManifest, ...] = ()
    unavailable_cause: ProducerUnavailableCause | None = None

    def __post_init__(self) -> None:
        complete = self.disposition is BundledSkillDefinitionsDisposition.COMPLETE
        if complete != (self.unavailable_cause is None):
            raise ValueError("bundled Skill definitions disposition conflicts")
        if not complete and self.candidates:
            raise ValueError("unavailable bundled definitions contain partial facts")
        if self.unavailable_cause is not None and (
            self.unavailable_cause.producer_kind is not SkillProducerKind.BUNDLED
        ):
            raise ValueError("bundled definitions contain a foreign cause")
        if complete:
            names = tuple(item.name for item in self.candidates)
            if names != EXPECTED_BUNDLED_SKILL_NAMES:
                raise ValueError("complete bundled definitions do not match inventory")
            if any(not isinstance(item.origin, BundledSkillOrigin) for item in self.candidates):
                raise ValueError("bundled definitions contain a non-bundled candidate")


class BundledSkillDistributionBindingOwner:
    """Process-local held descriptor for one installed Pulsara distribution."""

    def __init__(self, *, _test_resource_root: Path | None = None) -> None:
        self._descriptor: int | None = None
        self._root_path: Path | None = None
        self._identity: tuple[int, int] | None = None
        self._construction_cause: ProducerUnavailableCause | None = None
        self._closed = False
        descriptor: int | None = None
        try:
            resource = (
                _test_resource_root
                if _test_resource_root is not None
                else resources.files("pulsara_agent").joinpath("bundled_skills")
            )
            if not isinstance(resource, Path):
                raise TypeError("bundled resources are not filesystem-backed")
            path = prepare_local_source_path(resource)
            if not path.is_absolute():
                raise ValueError("bundled resource root is not absolute")
            descriptor = open_absolute_directory_nofollow(path)
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise NotADirectoryError(path)
            self._descriptor = descriptor
            descriptor = None
            self._root_path = path
            self._identity = (metadata.st_dev, metadata.st_ino)
        except (MemoryError, OSError, RuntimeError, TypeError, ValueError):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self._construction_cause = _bundled_cause(
                SkillProducerUnavailableReason.BUNDLED_RESOURCE_UNAVAILABLE,
                (
                    skill_diagnostic(
                        SkillDiagnosticSeverity.ERROR,
                        SkillDiagnosticCode.BUNDLED_DEFINITIONS_UNAVAILABLE,
                        message="Installed Pulsara bundled Skill resources are unavailable",
                    ),
                ),
            )

    @property
    def is_bound(self) -> bool:
        return self._descriptor is not None and not self._closed

    @property
    def root_path(self) -> Path | None:
        return self._root_path

    @property
    def construction_cause(self) -> ProducerUnavailableCause | None:
        return self._construction_cause

    def borrowed_binding(self) -> tuple[Path, int, tuple[int, int]] | None:
        if self._closed:
            raise RuntimeError("bundled Skill distribution binding is closed")
        if self._descriptor is None or self._root_path is None or self._identity is None:
            return None
        return self._root_path, self._descriptor, self._identity

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    async def aclose(self) -> None:
        self.close()

    def __enter__(self) -> BundledSkillDistributionBindingOwner:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class BundledSkillDefinitionProducer:
    """Observe the exact official definitions through a borrowed process binding."""

    def __init__(self, binding_owner: BundledSkillDistributionBindingOwner) -> None:
        if not isinstance(binding_owner, BundledSkillDistributionBindingOwner):
            raise TypeError("bundled producer requires its physical binding owner")
        self._binding_owner = binding_owner

    def observe(
        self,
        *,
        deadline_monotonic: float | None = None,
        cancellation: SkillObservationCancellationPort | None = None,
    ) -> FrozenBundledSkillDefinitions:
        check_skill_deadline(deadline_monotonic, cancellation)
        held_roots = []
        try:
            construction = self._binding_owner.construction_cause
            if construction is not None:
                return FrozenBundledSkillDefinitions(
                    BundledSkillDefinitionsDisposition.UNAVAILABLE,
                    unavailable_cause=construction,
                )
            borrowed = self._binding_owner.borrowed_binding()
            if borrowed is None:
                return FrozenBundledSkillDefinitions(
                    BundledSkillDefinitionsDisposition.UNAVAILABLE,
                    unavailable_cause=_bundled_cause(
                        SkillProducerUnavailableReason.BUNDLED_RESOURCE_UNAVAILABLE,
                        (
                            skill_diagnostic(
                                SkillDiagnosticSeverity.ERROR,
                                SkillDiagnosticCode.BUNDLED_DEFINITIONS_UNAVAILABLE,
                            ),
                        ),
                    ),
                )
            root_path, descriptor, identity = borrowed
            held = observe_skill_root(
                root_path,
                root_path,
                maximum_direct_child_directories=len(EXPECTED_BUNDLED_SKILL_NAMES),
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
                bound_descriptor=descriptor,
                bound_identity=identity,
                classify_nonregular_skill_document_as_missing=True,
            )
            held_roots.append(held)
            classification = classify_bundled_skill_inventory(
                BundledInventoryEntry(
                    child.evidence.name,
                    child.evidence.file_type == stat.S_IFDIR,
                    child.evidence.skill_file_identity is not None,
                )
                for child in held.children
            )
            if not classification.valid:
                return FrozenBundledSkillDefinitions(
                    BundledSkillDefinitionsDisposition.UNAVAILABLE,
                    unavailable_cause=_bundled_cause(
                        SkillProducerUnavailableReason.BUNDLED_INVENTORY_MISMATCH,
                        tuple(
                            skill_diagnostic(
                                SkillDiagnosticSeverity.ERROR,
                                SkillDiagnosticCode.BUNDLED_INVENTORY_MISMATCH,
                                message=message,
                            )
                            for message in classification.diagnostics
                        ),
                    ),
                )
            by_name = {item.evidence.name: item for item in held.children}
            candidates: list[SkillManifest] = []
            for name in EXPECTED_BUNDLED_SKILL_NAMES:
                child = by_name[name]
                data = read_observed_skill_document(
                    child,
                    maximum=MAX_SKILL_FILE_BYTES,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=cancellation,
                )
                skill_path = root_path / name / SKILL_FILE_NAME
                parsed = parse_skill_document(data)
                placement = (
                    validate_skill_candidate_placement(parsed.parsed, name)
                    if parsed.parsed is not None
                    else None
                )
                diagnostics = tuple(
                    diagnostic_at(item, skill_path)
                    for item in (
                        *parsed.diagnostics,
                        *((placement.diagnostics) if placement is not None else ()),
                    )
                )
                if parsed.parsed is None or (
                    placement is not None and not placement.valid
                ):
                    return FrozenBundledSkillDefinitions(
                        BundledSkillDefinitionsDisposition.UNAVAILABLE,
                        unavailable_cause=_bundled_cause(
                            SkillProducerUnavailableReason.BUNDLED_DEFINITION_INVALID,
                            (
                                *diagnostics,
                                skill_diagnostic(
                                    SkillDiagnosticSeverity.ERROR,
                                    SkillDiagnosticCode.BUNDLED_DEFINITIONS_UNAVAILABLE,
                                    path=skill_path,
                                ),
                            ),
                        ),
                    )
                if len(str(skill_path).encode("utf-8")) > MAX_SKILL_LOCATION_BYTES:
                    return FrozenBundledSkillDefinitions(
                        BundledSkillDefinitionsDisposition.UNAVAILABLE,
                        unavailable_cause=_bundled_cause(
                            SkillProducerUnavailableReason.BUNDLED_DEFINITION_INVALID,
                            (
                                skill_diagnostic(
                                    SkillDiagnosticSeverity.WARNING,
                                    SkillDiagnosticCode.LOCATION_OVERBOUND,
                                    path=skill_path,
                                ),
                                skill_diagnostic(
                                    SkillDiagnosticSeverity.ERROR,
                                    SkillDiagnosticCode.BUNDLED_DEFINITIONS_UNAVAILABLE,
                                    path=skill_path,
                                ),
                            ),
                        ),
                    )
                candidates.append(
                    enrich_skill_document(
                        parsed.parsed,
                        path=skill_path,
                        location=str(skill_path),
                        origin=BundledSkillOrigin(f"bundled_skills/{name}"),
                        diagnostic_codes=tuple(item.code for item in diagnostics),
                    )
                )
            revalidate_skill_roots(
                held_roots,
                maximum_direct_child_directories=len(EXPECTED_BUNDLED_SKILL_NAMES),
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            )
            check_skill_deadline(deadline_monotonic, cancellation)
            return FrozenBundledSkillDefinitions(
                BundledSkillDefinitionsDisposition.COMPLETE,
                candidates=tuple(candidates),
            )
        except TimeoutError:
            raise
        except SkillObservationError as exc:
            inventory_defect = exc.code in {
                SkillDiagnosticCode.DIRECT_CHILD_BOUND_EXCEEDED,
                SkillDiagnosticCode.DIRECTORY_ESCAPE,
                SkillDiagnosticCode.FILE_ESCAPE,
                SkillDiagnosticCode.ROOT_NOT_DIRECTORY,
            }
            return FrozenBundledSkillDefinitions(
                BundledSkillDefinitionsDisposition.UNAVAILABLE,
                unavailable_cause=_bundled_cause(
                    (
                        SkillProducerUnavailableReason.BUNDLED_INVENTORY_MISMATCH
                        if inventory_defect
                        else SkillProducerUnavailableReason.BUNDLED_DISCOVERY_RACED
                    ),
                    (
                        skill_diagnostic(
                            SkillDiagnosticSeverity.ERROR,
                            (
                                SkillDiagnosticCode.BUNDLED_INVENTORY_MISMATCH
                                if inventory_defect
                                else exc.code
                            ),
                        ),
                    ),
                ),
            )
        except (MemoryError, OSError, RuntimeError):
            return FrozenBundledSkillDefinitions(
                BundledSkillDefinitionsDisposition.UNAVAILABLE,
                unavailable_cause=_bundled_cause(
                    SkillProducerUnavailableReason.BUNDLED_DISCOVERY_RACED,
                    (
                        skill_diagnostic(
                            SkillDiagnosticSeverity.ERROR,
                            SkillDiagnosticCode.BUNDLED_DEFINITIONS_UNAVAILABLE,
                        ),
                    ),
                ),
            )
        finally:
            close_skill_roots(held_roots)


def _bundled_cause(
    reason: SkillProducerUnavailableReason,
    diagnostics: tuple[SkillDiagnostic, ...],
) -> ProducerUnavailableCause:
    return ProducerUnavailableCause(
        SkillProducerKind.BUNDLED,
        reason,
        tuple(
            sorted(
                diagnostics,
                key=lambda item: (
                    item.code.value,
                    "" if item.path is None else item.path.as_posix(),
                    item.message,
                ),
            )
        ),
    )


__all__ = [
    "BundledInventoryClassification",
    "BundledInventoryEntry",
    "BundledSkillDefinitionProducer",
    "BundledSkillDefinitionsDisposition",
    "BundledSkillDistributionBindingOwner",
    "EXPECTED_BUNDLED_SKILL_NAMES",
    "FrozenBundledSkillDefinitions",
    "classify_bundled_skill_inventory",
]
