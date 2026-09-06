"""Closed contracts for the local Agent Plugins 1.0 product boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeAlias

from pulsara_agent.hooks.contracts import HookTrustDisposition
from pulsara_agent.plugins.mcp_connection import PluginMcpConnectionOverlay, PluginConnectionReview
from pulsara_agent.plugins.connection_inputs import PluginConnectionInputs


PLUGIN_MANIFEST_SCHEMA_ID = (
    "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
)
PLUGIN_MCP_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
PLUGIN_INSTANCE_STATE_CONTRACT_ID = "pulsara.plugin-instance-state.v1"


class PluginCancellationPort(Protocol):
    def cancellation_requested(self) -> bool: ...


class NeverCancelPluginOperation:
    def cancellation_requested(self) -> bool:
        return False


class PluginScopeKind(StrEnum):
    USER = "USER"
    WORKSPACE = "WORKSPACE"


class ExternalProcessAcceptance(StrEnum):
    ACCEPTED = "ACCEPTED"


class PluginValidationDisposition(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class PluginInstallDisposition(StrEnum):
    INSTALLED = "INSTALLED"
    REPLACED = "REPLACED"
    ALREADY_PRESENT = "ALREADY_PRESENT"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    ACK_UNKNOWN = "ACK_UNKNOWN"
    CLEANUP_UNAVAILABLE = "CLEANUP_UNAVAILABLE"


class PluginEnablementDisposition(StrEnum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    ALREADY_ENABLED = "ALREADY_ENABLED"
    ALREADY_DISABLED = "ALREADY_DISABLED"
    NOT_FOUND = "NOT_FOUND"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    ACK_UNKNOWN = "ACK_UNKNOWN"


class PluginRemovalDisposition(StrEnum):
    REMOVED = "REMOVED"
    NOT_FOUND = "NOT_FOUND"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    ACK_UNKNOWN = "ACK_UNKNOWN"


class PluginInspectionDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


class PluginInspectionAbortReason(StrEnum):
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class PluginInspectionComponentDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


class PluginMcpNormalizationDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


class PluginGcDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class PluginComponentObservationDisposition(StrEnum):
    MISSING = "MISSING"
    COMPLETE = "COMPLETE"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


class EnabledPluginViewDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


class PluginCleanupLocationStatus(StrEnum):
    ABSENT = "ABSENT"
    STAGE_ONLY = "STAGE_ONLY"
    UNREFERENCED_VERSION = "UNREFERENCED_VERSION"
    UNKNOWN = "UNKNOWN"


class PluginGcLocationStatus(StrEnum):
    ABSENT = "ABSENT"
    STAGE_ONLY = "STAGE_ONLY"
    UNREFERENCED_VERSION = "UNREFERENCED_VERSION"
    STATE_TEMP = "STATE_TEMP"
    UNKNOWN = "UNKNOWN"


class PluginGcRefKind(StrEnum):
    VERSION = "VERSION"
    STAGE = "STAGE"
    STATE_TEMP = "STATE_TEMP"


class PluginDiagnosticSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class PluginDiagnosticCode(StrEnum):
    HOME_CONFIGURATION_INVALID = "plugin_home_configuration_invalid"
    WORKSPACE_REQUIRED = "plugin_workspace_required"
    SOURCE_NOT_DIRECTORY = "plugin_source_not_directory"
    SOURCE_FINAL_SYMLINK = "plugin_source_final_symlink"
    SOURCE_TREE_SYMLINK = "plugin_source_tree_symlink"
    SOURCE_SPECIAL_FILE = "plugin_source_special_file"
    SOURCE_ESCAPE = "plugin_source_escape"
    SOURCE_UNAVAILABLE = "plugin_source_unavailable"
    SOURCE_RACED = "plugin_source_raced"
    SOURCE_CONTAINS_ACTIVE_API_KEY = "plugin_source_contains_active_api_key"
    MANIFEST_MISSING = "plugin_manifest_missing"
    MANIFEST_OVERBOUND = "plugin_manifest_overbound"
    MANIFEST_INVALID_UTF8 = "plugin_manifest_invalid_utf8"
    MANIFEST_INVALID_JSON = "plugin_manifest_invalid_json"
    MANIFEST_SCHEMA_UNSUPPORTED = "plugin_manifest_schema_unsupported"
    MANIFEST_INVALID = "plugin_manifest_invalid"
    MANIFEST_UNKNOWN_FIELD_IGNORED = "plugin_manifest_unknown_field_ignored"
    EXTENSIONS_FIELD_IGNORED = "plugin_extensions_field_ignored"
    PACKAGE_NOT_FOUND = "plugin_package_not_found"
    PACKAGE_ROOT_UNAVAILABLE = "plugin_package_root_unavailable"
    PACKAGE_ROOT_RACED = "plugin_package_root_raced"
    STAGING_UNAVAILABLE = "plugin_staging_unavailable"
    PUBLISH_CONFLICT = "plugin_publish_conflict"
    STATE_UNAVAILABLE = "plugin_state_unavailable"
    STATE_RACED = "plugin_state_raced"
    CLEANUP_UNAVAILABLE = "plugin_cleanup_unavailable"
    PACKAGE_IN_USE = "plugin_package_in_use"
    DATA_ROOT_UNAVAILABLE = "plugin_data_root_unavailable"
    DATA_ROOT_RACED = "plugin_data_root_raced"
    COMPONENT_KIND_INVALID = "plugin_component_kind_invalid"
    MCP_COMPONENT_INVALID = "plugin_mcp_component_invalid"
    MCP_SERVER_INVALID = "plugin_mcp_server_invalid"
    MCP_TRANSPORT_UNSUPPORTED = "plugin_mcp_transport_unsupported"
    MCP_SERVER_ID_COLLISION = "plugin_mcp_server_id_collision"
    MCP_CONFIGURED_BOUND_EXCEEDED = "plugin_mcp_configured_bound_exceeded"
    VIEW_UNAVAILABLE = "plugin_view_unavailable"
    RELOAD_PARTIAL = "plugin_reload_partial"


if len(PluginDiagnosticCode) != 37:
    raise RuntimeError("Plugin diagnostic vocabulary must contain exactly 37 codes")


_INFO_DIAGNOSTICS = frozenset(
    {
        PluginDiagnosticCode.MANIFEST_UNKNOWN_FIELD_IGNORED,
        PluginDiagnosticCode.EXTENSIONS_FIELD_IGNORED,
        PluginDiagnosticCode.PACKAGE_IN_USE,
        PluginDiagnosticCode.MCP_TRANSPORT_UNSUPPORTED,
    }
)
_WARNING_DIAGNOSTICS = frozenset(
    {
        PluginDiagnosticCode.PACKAGE_NOT_FOUND,
        PluginDiagnosticCode.CLEANUP_UNAVAILABLE,
        PluginDiagnosticCode.COMPONENT_KIND_INVALID,
        PluginDiagnosticCode.MCP_COMPONENT_INVALID,
        PluginDiagnosticCode.MCP_SERVER_INVALID,
        PluginDiagnosticCode.MCP_SERVER_ID_COLLISION,
        PluginDiagnosticCode.MCP_CONFIGURED_BOUND_EXCEEDED,
        PluginDiagnosticCode.RELOAD_PARTIAL,
    }
)
_ERROR_DIAGNOSTICS = frozenset(PluginDiagnosticCode) - (
    _INFO_DIAGNOSTICS | _WARNING_DIAGNOSTICS
)
if (
    _INFO_DIAGNOSTICS & _WARNING_DIAGNOSTICS
    or _INFO_DIAGNOSTICS & _ERROR_DIAGNOSTICS
    or _WARNING_DIAGNOSTICS & _ERROR_DIAGNOSTICS
    or _INFO_DIAGNOSTICS | _WARNING_DIAGNOSTICS | _ERROR_DIAGNOSTICS
    != frozenset(PluginDiagnosticCode)
):
    raise RuntimeError("Plugin diagnostic severity partition is not exact")


def plugin_diagnostic_severity(
    code: PluginDiagnosticCode,
) -> PluginDiagnosticSeverity:
    if code in _INFO_DIAGNOSTICS:
        return PluginDiagnosticSeverity.INFO
    if code in _WARNING_DIAGNOSTICS:
        return PluginDiagnosticSeverity.WARNING
    if code in _ERROR_DIAGNOSTICS:
        return PluginDiagnosticSeverity.ERROR
    raise TypeError("Plugin diagnostic code union is open")


@dataclass(frozen=True, slots=True)
class PluginDiagnostic:
    code: PluginDiagnosticCode
    message: str
    path: str | None = None
    component: str | None = None
    severity: PluginDiagnosticSeverity = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.code, PluginDiagnosticCode) or not self.message:
            raise ValueError("Plugin diagnostic is incomplete")
        object.__setattr__(self, "severity", plugin_diagnostic_severity(self.code))


@dataclass(frozen=True, slots=True)
class PluginAuthor:
    name: str | None = None
    email: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class PluginManifest:
    schema: str
    name: str
    version: str | None
    description: str | None
    author: PluginAuthor | None
    homepage: str | None
    repository: str | None
    license: str | None
    keywords: tuple[str, ...]
    extension_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema != PLUGIN_MANIFEST_SCHEMA_ID or not self.name:
            raise ValueError("Plugin manifest identity is invalid")
        if self.extension_names != tuple(sorted(self.extension_names)):
            raise ValueError("Plugin extension names are not deterministic")


@dataclass(frozen=True, slots=True)
class PluginSkillSummary:
    name: str
    description: str
    location: str


class PluginMcpTransportSummaryKind(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable-http"
    SSE = "sse"


@dataclass(frozen=True, slots=True)
class PluginMcpStdioSummary:
    local_server_id: str
    command: str
    args: tuple[str, ...]
    cwd: str | None
    environment: tuple[tuple[str, str], ...]
    connection_inputs: PluginConnectionInputs = field(default_factory=PluginConnectionInputs, kw_only=True)
    kind: PluginMcpTransportSummaryKind = field(
        default=PluginMcpTransportSummaryKind.STDIO, init=False
    )


@dataclass(frozen=True, slots=True)
class PluginMcpHttpSummary:
    local_server_id: str
    endpoint: str
    public_headers: tuple[tuple[str, str], ...]
    connection_inputs: PluginConnectionInputs = field(default_factory=PluginConnectionInputs, kw_only=True)
    kind: PluginMcpTransportSummaryKind = field(
        default=PluginMcpTransportSummaryKind.STREAMABLE_HTTP, init=False
    )


@dataclass(frozen=True, slots=True)
class PluginMcpSseSummary:
    local_server_id: str
    endpoint: str
    public_headers: tuple[tuple[str, str], ...]
    connection_inputs: PluginConnectionInputs = field(default_factory=PluginConnectionInputs, kw_only=True)
    kind: PluginMcpTransportSummaryKind = field(
        default=PluginMcpTransportSummaryKind.SSE, init=False
    )


PluginMcpServerSummary: TypeAlias = (
    PluginMcpStdioSummary | PluginMcpHttpSummary | PluginMcpSseSummary
)


@dataclass(frozen=True, slots=True)
class PluginHookDefinitionSummary:
    event: str
    matcher: str
    command: str
    command_windows: str | None
    timeout_seconds: int
    asynchronous: bool
    status_message: str | None


@dataclass(frozen=True, slots=True)
class PluginComponentSummary:
    disposition: PluginComponentObservationDisposition
    skills: tuple[PluginSkillSummary, ...] = ()
    mcp_servers: tuple[PluginMcpServerSummary, ...] = ()
    hook_definitions: tuple[PluginHookDefinitionSummary, ...] = ()
    diagnostics: tuple[object, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class PluginValidationSummary:
    manifest: PluginManifest
    skills: PluginComponentSummary
    mcp: PluginComponentSummary
    hooks: PluginComponentSummary

    def __post_init__(self) -> None:
        if self.skills.skills != tuple(
            sorted(self.skills.skills, key=lambda item: item.name)
        ):
            raise ValueError("Plugin Skill summary is not deterministic")
        if self.mcp.mcp_servers != tuple(
            sorted(self.mcp.mcp_servers, key=lambda item: item.local_server_id)
        ):
            raise ValueError("Plugin MCP summary is not deterministic")


@dataclass(frozen=True, slots=True)
class ValidateLocalPluginSourceRequest:
    source_path: Path
    deadline_monotonic: float
    source_format: str = field(default="native", kw_only=True)
    import_classifications: tuple[tuple[str, str], ...] = field(default=(), kw_only=True)
    import_public_values: tuple[tuple[str, str], ...] = field(default=(), kw_only=True)
    cancellation: PluginCancellationPort = field(
        default_factory=NeverCancelPluginOperation, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.source_path.is_absolute() or self.deadline_monotonic <= 0:
            raise ValueError("Plugin validation request is not frozen")


@dataclass(frozen=True, slots=True)
class InstallLocalPluginRequest:
    source_path: Path
    scope: PluginScopeKind
    deadline_monotonic: float
    source_format: str = field(default="native", kw_only=True)
    import_classifications: tuple[tuple[str, str], ...] = field(default=(), kw_only=True)
    import_public_values: tuple[tuple[str, str], ...] = field(default=(), kw_only=True)
    workspace_root: Path | None = None
    replace: bool = False
    prepared_current: PreparedPluginInstanceObservation | None = field(default=None, repr=False, kw_only=True)
    cancellation: PluginCancellationPort = field(
        default_factory=NeverCancelPluginOperation, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.source_path.is_absolute() or self.deadline_monotonic <= 0:
            raise ValueError("Plugin install request is not frozen")
        if (self.scope is PluginScopeKind.WORKSPACE) != (
            self.workspace_root is not None
        ):
            raise ValueError("Plugin install workspace conflicts with scope")
        if self.workspace_root is not None and not self.workspace_root.is_absolute():
            raise ValueError("Plugin install workspace must be absolute")


@dataclass(frozen=True, slots=True)
class SetLocalPluginEnabledRequest:
    scope: PluginScopeKind
    plugin_id: str
    enabled: bool
    expected_current_package_install_id: str
    deadline_monotonic: float
    connection_review: tuple[PluginConnectionReview, ...] = field(kw_only=True)
    prepared_current: PreparedPluginInstanceObservation | None = field(default=None, repr=False, kw_only=True)
    workspace_root: Path | None = None
    external_process_acceptance: ExternalProcessAcceptance | None = None
    cancellation: PluginCancellationPort = field(
        default_factory=NeverCancelPluginOperation, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _validate_instance_request(
            self.scope, self.plugin_id, self.workspace_root, self.deadline_monotonic
        )
        if not _valid_package_install_id(self.expected_current_package_install_id):
            raise ValueError("expected Plugin package install id is invalid")
        if self.enabled != (
            self.external_process_acceptance is ExternalProcessAcceptance.ACCEPTED
        ):
            if self.enabled:
                raise ValueError("Plugin enable requires external-process acceptance")
            if self.external_process_acceptance is not None:
                raise ValueError("Plugin disable cannot carry process acceptance")


@dataclass(frozen=True, slots=True)
class RemoveLocalPluginRequest:
    scope: PluginScopeKind
    plugin_id: str
    deadline_monotonic: float
    expected_current_package_install_id: str
    prepared_current: PreparedPluginInstanceObservation | None = field(default=None, repr=False, kw_only=True)
    workspace_root: Path | None = None
    cancellation: PluginCancellationPort = field(
        default_factory=NeverCancelPluginOperation, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _validate_instance_request(
            self.scope, self.plugin_id, self.workspace_root, self.deadline_monotonic
        )
        if not _valid_package_install_id(self.expected_current_package_install_id):
            raise ValueError("expected Plugin removal package id is invalid")


@dataclass(frozen=True, slots=True)
class InspectLocalPluginsRequest:
    deadline_monotonic: float
    workspace_root: Path | None = None
    cancellation: PluginCancellationPort = field(
        default_factory=NeverCancelPluginOperation, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.deadline_monotonic <= 0 or (
            self.workspace_root is not None and not self.workspace_root.is_absolute()
        ):
            raise ValueError("Plugin inspection request is not frozen")


@dataclass(frozen=True, slots=True)
class GcLocalPluginPackagesRequest:
    deadline_monotonic: float
    workspace_root: Path | None = None
    cancellation: PluginCancellationPort = field(
        default_factory=NeverCancelPluginOperation, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.deadline_monotonic <= 0 or (
            self.workspace_root is not None and not self.workspace_root.is_absolute()
        ):
            raise ValueError("Plugin GC request is not frozen")


@dataclass(frozen=True, slots=True)
class ValidPluginValidationOutcome:
    source_path: Path
    summary: PluginValidationSummary
    diagnostics: tuple[object, ...] = ()
    disposition: PluginValidationDisposition = field(
        default=PluginValidationDisposition.VALID, init=False
    )


@dataclass(frozen=True, slots=True)
class InvalidPluginValidationOutcome:
    source_path: Path
    diagnostics: tuple[object, ...]
    disposition: PluginValidationDisposition = field(
        default=PluginValidationDisposition.INVALID, init=False
    )

    def __post_init__(self) -> None:
        if not self.diagnostics:
            raise ValueError("invalid Plugin validation lacks diagnostics")


@dataclass(frozen=True, slots=True)
class UnavailablePluginValidationOutcome:
    source_path: Path
    diagnostics: tuple[PluginDiagnostic, ...]
    disposition: PluginValidationDisposition = field(
        default=PluginValidationDisposition.UNAVAILABLE, init=False
    )

    def __post_init__(self) -> None:
        if not self.diagnostics:
            raise ValueError("unavailable Plugin validation lacks diagnostics")


@dataclass(frozen=True, slots=True)
class AbortedPluginValidationOutcome:
    source_path: Path
    disposition: PluginValidationDisposition

    def __post_init__(self) -> None:
        if self.disposition not in {
            PluginValidationDisposition.CANCELLED,
            PluginValidationDisposition.TIMED_OUT,
        }:
            raise ValueError("Plugin validation abort disposition is invalid")


PluginValidationOutcome: TypeAlias = (
    ValidPluginValidationOutcome
    | InvalidPluginValidationOutcome
    | UnavailablePluginValidationOutcome
    | AbortedPluginValidationOutcome
)


@dataclass(frozen=True, slots=True)
class PluginInstanceIdentity:
    scope: PluginScopeKind
    plugin_id: str
    workspace_state_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, PluginScopeKind) or not _valid_plugin_id(
            self.plugin_id
        ):
            raise ValueError("Plugin instance identity is invalid")
        if (self.scope is PluginScopeKind.WORKSPACE) != (
            self.workspace_state_key is not None
        ):
            raise ValueError("Plugin instance identity workspace conflicts")
        if self.workspace_state_key is not None and not _valid_workspace_state_key(
            self.workspace_state_key
        ):
            raise ValueError("Plugin workspace state key is invalid")


@dataclass(frozen=True, slots=True)
class PluginInstanceState:
    plugin_id: str
    scope: PluginScopeKind
    current_package_install_id: str
    enabled: bool
    workspace_state_key: str | None = None
    mcp_connection_overlays: tuple[PluginMcpConnectionOverlay, ...] = ()
    contract_id: str = field(default=PLUGIN_INSTANCE_STATE_CONTRACT_ID, init=False)

    def __post_init__(self) -> None:
        if not _valid_plugin_id(self.plugin_id) or not _valid_package_install_id(
            self.current_package_install_id
        ):
            raise ValueError("Plugin instance state identity is invalid")
        if (self.scope is PluginScopeKind.WORKSPACE) != (
            self.workspace_state_key is not None
        ):
            raise ValueError("Plugin instance state workspace conflicts")
        if self.workspace_state_key is not None and not _valid_workspace_state_key(
            self.workspace_state_key
        ):
            raise ValueError("Plugin instance state workspace key is invalid")
        names = tuple(item.local_server_id for item in self.mcp_connection_overlays)
        if names != tuple(sorted(set(names))):
            raise ValueError("Plugin MCP overlays must be unique and ordered")


@dataclass(frozen=True, slots=True)
class PreparedPluginInstanceObservation:
    """Optional model-preparation guard, not another user enablement receipt.

    A user control-plane mutation already authorizes its physical effects. A
    model operation instead has a permission verdict derived from this exact
    prior state (including disabled/process-free cases). Rejoin it under the
    existing instance lock so a concurrent enable cannot silently expand those
    effects. None inside this value explicitly observes an absent instance.
    """

    identity: PluginInstanceIdentity
    current: PluginInstanceState | None

    def __post_init__(self):
        if self.current is not None and self.identity != PluginInstanceIdentity(
            self.current.scope, self.current.plugin_id, self.current.workspace_state_key,
        ):
            raise ValueError("prepared Plugin state belongs to another instance")


@dataclass(frozen=True, slots=True)
class SuccessfulPluginInstallOutcome:
    disposition: PluginInstallDisposition
    identity: PluginInstanceIdentity
    package_install_id: str
    summary: PluginValidationSummary
    diagnostics: tuple[object, ...]
    cleanup_attention: bool = False
    enabled: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.disposition not in {
            PluginInstallDisposition.INSTALLED,
            PluginInstallDisposition.REPLACED,
        } or not _valid_package_install_id(self.package_install_id):
            raise ValueError("successful Plugin install outcome is invalid")


@dataclass(frozen=True, slots=True)
class AlreadyPresentPluginInstallOutcome:
    identity: PluginInstanceIdentity
    package_install_id: str
    enabled: bool
    summary: PluginValidationSummary
    disposition: PluginInstallDisposition = field(
        default=PluginInstallDisposition.ALREADY_PRESENT, init=False
    )


@dataclass(frozen=True, slots=True)
class FailedPluginInstallOutcome:
    disposition: PluginInstallDisposition
    source_path: Path
    identity: PluginInstanceIdentity | None = None
    attempted_package_install_id: str | None = None
    diagnostics: tuple[object, ...] = ()
    intended_enabled: bool | None = None
    last_known_cut: str | None = None

    def __post_init__(self) -> None:
        if self.disposition not in {
            PluginInstallDisposition.INVALID,
            PluginInstallDisposition.UNAVAILABLE,
            PluginInstallDisposition.CANCELLED,
            PluginInstallDisposition.TIMED_OUT,
            PluginInstallDisposition.ACK_UNKNOWN,
        }:
            raise ValueError("Plugin install failure disposition is invalid")
        if self.disposition is PluginInstallDisposition.INVALID and not self.diagnostics:
            raise ValueError("invalid Plugin install lacks validation diagnostics")
        if self.disposition is PluginInstallDisposition.ACK_UNKNOWN and (
            self.attempted_package_install_id is None
            or self.intended_enabled is None
            or self.last_known_cut is None
        ):
            raise ValueError("Plugin install ACK_UNKNOWN lacks cut facts")


@dataclass(frozen=True, slots=True)
class CleanupUnavailablePluginInstallOutcome:
    prior: "NonCleanupPluginInstallOutcome"
    attempted_path: Path
    location_status: PluginCleanupLocationStatus
    diagnostic: PluginDiagnostic
    disposition: PluginInstallDisposition = field(
        default=PluginInstallDisposition.CLEANUP_UNAVAILABLE, init=False
    )


NonCleanupPluginInstallOutcome: TypeAlias = (
    SuccessfulPluginInstallOutcome
    | AlreadyPresentPluginInstallOutcome
    | FailedPluginInstallOutcome
)
PluginInstallOutcome: TypeAlias = (
    NonCleanupPluginInstallOutcome | CleanupUnavailablePluginInstallOutcome
)


@dataclass(frozen=True, slots=True)
class SettledPluginEnablementOutcome:
    disposition: PluginEnablementDisposition
    identity: PluginInstanceIdentity
    package_install_id: str
    enabled: bool

    def __post_init__(self) -> None:
        if self.disposition not in {
            PluginEnablementDisposition.ENABLED,
            PluginEnablementDisposition.DISABLED,
            PluginEnablementDisposition.ALREADY_ENABLED,
            PluginEnablementDisposition.ALREADY_DISABLED,
        }:
            raise ValueError("settled Plugin enablement disposition is invalid")


@dataclass(frozen=True, slots=True)
class FailedPluginEnablementOutcome:
    disposition: PluginEnablementDisposition
    identity: PluginInstanceIdentity
    desired_enabled: bool
    expected_package_install_id: str | None = None
    observed_package_install_id: str | None = None
    last_known_cut: str | None = None
    diagnostics: tuple[PluginDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if self.disposition not in {
            PluginEnablementDisposition.NOT_FOUND,
            PluginEnablementDisposition.STALE,
            PluginEnablementDisposition.UNAVAILABLE,
            PluginEnablementDisposition.CANCELLED,
            PluginEnablementDisposition.TIMED_OUT,
            PluginEnablementDisposition.ACK_UNKNOWN,
        }:
            raise ValueError("Plugin enablement failure disposition is invalid")
        if self.disposition is PluginEnablementDisposition.STALE and (
            self.expected_package_install_id is None
            or self.observed_package_install_id is None
        ):
            raise ValueError("stale Plugin enablement lacks package ids")
        if self.disposition is PluginEnablementDisposition.ACK_UNKNOWN and (
            self.expected_package_install_id is None
            or self.observed_package_install_id is None
            or self.last_known_cut is None
        ):
            raise ValueError("Plugin enablement ACK_UNKNOWN lacks cut facts")
        if self.last_known_cut is not None and (
            self.disposition is not PluginEnablementDisposition.ACK_UNKNOWN
        ):
            raise ValueError("settled Plugin enablement failure carries a cut fact")


PluginEnablementOutcome: TypeAlias = (
    SettledPluginEnablementOutcome | FailedPluginEnablementOutcome
)


@dataclass(frozen=True, slots=True)
class RemovedPluginOutcome:
    identity: PluginInstanceIdentity
    prior_package_install_id: str
    cleanup_attention: bool = False
    disposition: PluginRemovalDisposition = field(
        default=PluginRemovalDisposition.REMOVED, init=False
    )


@dataclass(frozen=True, slots=True)
class FailedPluginRemovalOutcome:
    disposition: PluginRemovalDisposition
    identity: PluginInstanceIdentity
    prior_package_install_id: str | None = None
    last_known_cut: str | None = None
    diagnostics: tuple[PluginDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if self.disposition not in {
            PluginRemovalDisposition.NOT_FOUND,
            PluginRemovalDisposition.STALE,
            PluginRemovalDisposition.UNAVAILABLE,
            PluginRemovalDisposition.CANCELLED,
            PluginRemovalDisposition.TIMED_OUT,
            PluginRemovalDisposition.ACK_UNKNOWN,
        }:
            raise ValueError("Plugin removal failure disposition is invalid")
        if self.disposition is PluginRemovalDisposition.ACK_UNKNOWN and (
            self.prior_package_install_id is None or self.last_known_cut is None
        ):
            raise ValueError("Plugin removal ACK_UNKNOWN lacks cut facts")
        if self.last_known_cut is not None and (
            self.disposition is not PluginRemovalDisposition.ACK_UNKNOWN
        ):
            raise ValueError("settled Plugin removal failure carries a cut fact")


PluginRemovalOutcome: TypeAlias = RemovedPluginOutcome | FailedPluginRemovalOutcome


@dataclass(frozen=True, slots=True)
class PluginInstanceInspection:
    identity: PluginInstanceIdentity
    package_install_id: str
    enabled: bool
    package_root: Path
    data_root: Path
    summary: PluginValidationSummary
    diagnostics: tuple[object, ...]
    package_in_use: bool
    effective_skill_names: tuple[str, ...] = ()
    effective_mcp_server_ids: tuple[str, ...] = ()
    effective_hook: bool = False
    effective_hook_definition_count: int = 0
    effective_hook_trust_disposition: HookTrustDisposition | None = None
    mcp_connection_overlays: tuple[PluginMcpConnectionOverlay, ...] = ()

    def __post_init__(self) -> None:
        for values, label in (
            (self.effective_skill_names, "Skill"),
            (self.effective_mcp_server_ids, "MCP"),
        ):
            if values != tuple(sorted(values)) or len(values) != len(set(values)):
                raise ValueError(f"effective Plugin {label} summary is not unique")
        if self.effective_hook:
            if self.effective_hook_trust_disposition is None:
                raise ValueError("effective Plugin Hook summary lacks trust")
        elif (
            self.effective_hook_definition_count
            or self.effective_hook_trust_disposition is not None
        ):
            raise ValueError("ineffective Plugin Hook carries an effective summary")
        if self.effective_hook_definition_count < 0:
            raise ValueError("effective Plugin Hook definition count is negative")


@dataclass(frozen=True, slots=True)
class PluginVersionInspection:
    identity: PluginInstanceIdentity
    package_install_id: str
    package_root: Path
    referenced: bool
    in_use: bool


@dataclass(frozen=True, slots=True)
class PluginInspectionOutcome:
    disposition: PluginInspectionDisposition
    instances: tuple[PluginInstanceInspection, ...]
    versions: tuple[PluginVersionInspection, ...]
    diagnostics: tuple[object, ...]
    physical_lifetime_anchors: tuple[object, ...] = field(
        default=(), repr=False, compare=False
    )
    skill_composition_disposition: PluginInspectionComponentDisposition = (
        PluginInspectionComponentDisposition.COMPLETE
    )
    mcp_composition_disposition: PluginInspectionComponentDisposition = (
        PluginInspectionComponentDisposition.COMPLETE
    )
    hook_composition_disposition: PluginInspectionComponentDisposition = (
        PluginInspectionComponentDisposition.COMPLETE
    )

    def __post_init__(self) -> None:
        if self.disposition is PluginInspectionDisposition.UNAVAILABLE and (
            self.instances or self.versions
        ):
            raise ValueError("unavailable Plugin inspection cannot be partial")
        if self.disposition is PluginInspectionDisposition.UNAVAILABLE and (
            not self.diagnostics or self.physical_lifetime_anchors
        ):
            raise ValueError("unavailable Plugin inspection lacks a closed cause")
        if any(
            not isinstance(item, PluginInspectionComponentDisposition)
            for item in (
                self.skill_composition_disposition,
                self.mcp_composition_disposition,
                self.hook_composition_disposition,
            )
        ):
            raise TypeError("Plugin inspection composition disposition is open")

    def close(self) -> None:
        for anchor in self.physical_lifetime_anchors:
            close = getattr(anchor, "close", None)
            if callable(close):
                close()


@dataclass(frozen=True, slots=True)
class PluginInspectionAbort:
    reason: PluginInspectionAbortReason


PluginInspectionResult: TypeAlias = PluginInspectionOutcome | PluginInspectionAbort


@dataclass(frozen=True, slots=True)
class PluginGcRef:
    kind: PluginGcRefKind
    path: Path
    package_install_id: str | None = None


@dataclass(frozen=True, slots=True)
class PluginGcProgress:
    ordered_removed: tuple[PluginGcRef, ...] = ()
    ordered_in_use: tuple[PluginGcRef, ...] = ()
    current_attempted_ref: PluginGcRef | None = None
    current_location_status: PluginGcLocationStatus | None = None
    unvisited_suffix: bool = True

    def __post_init__(self) -> None:
        if (self.current_attempted_ref is None) != (
            self.current_location_status is None
        ):
            raise ValueError("Plugin GC current progress is incomplete")
        for values in (self.ordered_removed, self.ordered_in_use):
            paths = tuple(item.path.as_posix() for item in values)
            if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
                raise ValueError("Plugin GC progress is not deterministic")


@dataclass(frozen=True, slots=True)
class PluginGcOutcome:
    disposition: PluginGcDisposition
    progress: PluginGcProgress
    diagnostics: tuple[PluginDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if self.disposition is PluginGcDisposition.COMPLETE and (
            self.progress.unvisited_suffix
            or self.progress.current_attempted_ref is not None
        ):
            raise ValueError("complete Plugin GC progress is incomplete")
        if (
            self.disposition is PluginGcDisposition.UNAVAILABLE
            and not self.diagnostics
        ):
            raise ValueError("unavailable Plugin GC lacks a diagnostic")
        if self.disposition in {
            PluginGcDisposition.CANCELLED,
            PluginGcDisposition.TIMED_OUT,
        } and self.diagnostics:
            raise ValueError("Plugin GC abort carries physical diagnostics")


def _validate_instance_request(
    scope: PluginScopeKind,
    plugin_id: str,
    workspace_root: Path | None,
    deadline_monotonic: float,
) -> None:
    if not isinstance(scope, PluginScopeKind) or not _valid_plugin_id(plugin_id):
        raise ValueError("Plugin instance request identity is invalid")
    if deadline_monotonic <= 0:
        raise ValueError("Plugin request deadline is invalid")
    if (scope is PluginScopeKind.WORKSPACE) != (workspace_root is not None):
        raise ValueError("Plugin request workspace conflicts with scope")
    if workspace_root is not None and not workspace_root.is_absolute():
        raise ValueError("Plugin request workspace must be absolute")


def _valid_plugin_id(value: str) -> bool:
    import re

    return bool(
        len(value.encode("utf-8")) <= 64
        and re.fullmatch(r"(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value)
    )


def _valid_package_install_id(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"pkg_[0-9a-f]{32}", value))


def _valid_workspace_state_key(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value))


__all__ = [name for name in globals() if not name.startswith("_")]
