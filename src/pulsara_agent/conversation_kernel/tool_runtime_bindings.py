"""Builtin descriptor-to-executor identity values for the direct tool owner."""

from __future__ import annotations

from dataclasses import dataclass
from pulsara_agent.capability.builtin_catalog import (
    BuiltinToolCatalogEntry,
    builtin_availability_requirement_identity_fingerprint,
    builtin_permission_contract_identity_fingerprint,
    execution_catalog_entry,
)
from pulsara_agent.ports.tool_registry import tool_binding_contract_identity_fingerprint
from pulsara_agent.primitives.model_call import sha256_fingerprint


_TERMINAL_PROCESS_ACTION_EFFECTS = (
    ("poll", "TERMINAL_OBSERVATION"),
    ("wait", "TERMINAL_OBSERVATION"),
    ("write", "TERMINAL_EFFECT"),
    ("submit", "TERMINAL_EFFECT"),
    ("close_stdin", "TERMINAL_EFFECT"),
    ("kill", "TERMINAL_EFFECT"),
)


@dataclass(frozen=True, slots=True)
class ProductionBuiltinExecutorBinding:
    """Exact descriptor-to-executor closure for one advertised builtin."""

    catalog_entry: BuiltinToolCatalogEntry
    executor_identity: str

    @property
    def tool_name(self) -> str:
        return self.catalog_entry.name


def _builtin_physical_effect_contract(
    entry: BuiltinToolCatalogEntry,
) -> object:
    return (
        {"actions": _TERMINAL_PROCESS_ACTION_EFFECTS}
        if entry.name == "manage_process"
        else {
            "default": (
                "TERMINAL_EFFECT"
                if entry.name == "exec_process"
                else entry.recovery_contract.severity
            )
        }
    )


def production_builtin_executor_binding_identity_fingerprint(
    binding: ProductionBuiltinExecutorBinding,
) -> str:
    """Derive the stable physical binding identity at its actual consumers."""

    entry = binding.catalog_entry
    descriptor = entry.descriptor
    contract = entry.binding_contract
    input_schema_fingerprint = sha256_fingerprint(
        "production-builtin-input-schema:v1", descriptor.input_schema
    )
    physical_effect_contract_fingerprint = sha256_fingerprint(
        "production-builtin-physical-effect-contract:v1",
        _builtin_physical_effect_contract(entry),
    )
    return sha256_fingerprint(
        "production-builtin-executor-binding:v1",
        {
            "tool_name": entry.name,
            "descriptor_id": descriptor.id,
            "descriptor_contract_version": contract.contract_version,
            "descriptor_fingerprint": descriptor.fingerprint(),
            "input_schema_fingerprint": input_schema_fingerprint,
            "binding_contract_fingerprint": (
                tool_binding_contract_identity_fingerprint(contract)
            ),
            "catalog_entry_fingerprint": entry.entry_fingerprint,
            "availability_requirement_fingerprint": (
                builtin_availability_requirement_identity_fingerprint(
                    entry.availability_requirement
                )
            ),
            "permission_contract_fingerprint": (
                builtin_permission_contract_identity_fingerprint(
                    entry.permission_contract
                )
            ),
            "execution_binding_kind": entry.execution_binding_kind.value,
            "is_read_only": descriptor.is_read_only,
            "permission_category": descriptor.permission_category,
            "physical_effect_contract_fingerprint": (
                physical_effect_contract_fingerprint
            ),
            "executor_identity": binding.executor_identity,
        },
    )


def _production_executor_binding(
    tool_name: str, executor_identity: str
) -> ProductionBuiltinExecutorBinding:
    if not tool_name or not executor_identity:
        raise ValueError("production executor binding identity is incomplete")
    entry = execution_catalog_entry(tool_name)
    descriptor = entry.descriptor
    contract = entry.binding_contract
    if descriptor.name != tool_name or contract.tool_name != tool_name:
        raise RuntimeError("builtin descriptor and executor name do not join")
    return ProductionBuiltinExecutorBinding(
        catalog_entry=entry,
        executor_identity=executor_identity,
    )


def _qualified_executor_identity(owner: object, tool_name: str) -> str:
    owner_type = type(owner)
    return f"{owner_type.__module__}.{owner_type.__qualname__}#{tool_name}"
