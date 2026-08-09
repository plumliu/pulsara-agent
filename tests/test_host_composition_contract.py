from __future__ import annotations

import pickle
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from pulsara_agent.host.composition_contract import build_host_runtime_admission
from pulsara_agent.host.core import HostCore
from pulsara_agent.workspace_identity import HostWorkspaceInput, resolve_workspace
from pulsara_agent.host.production_composition import ProductionHostComposition
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.runtime.mcp.installation import empty_mcp_installation
from pulsara_agent.runtime.mcp.supervisor import McpServerSupervisor
from pulsara_agent.runtime.permission import default_permission_policy
from pulsara_agent.runtime.terminal import OwnedTerminalRuntime
from pulsara_agent.settings import PulsaraSettings
from tests.support.host import (
    ComponentTestHostComposition,
    InMemorySessionManifestStore,
    TestHostProcessResourceLease as _TestHostProcessResourceLease,
    _FakeProjectionService,
)
from tests.support.model_call import test_llm_config
from tests.support.settings import compatibility_storage_config


def _settings() -> PulsaraSettings:
    return PulsaraSettings(
        llm=test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        ),
        storage=compatibility_storage_config(),
    )


def _admission(tmp_path: Path, *, generation: int = 1):
    workspace = resolve_workspace(
        HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=tmp_path,
            memory_domain_id="domain-a",
        )
    )
    return build_host_runtime_admission(
        settings=_settings(),
        workspace=workspace,
        runtime_session_id="runtime:1",
        graph_id="graph:1",
        model_role=ModelRole.PRO,
        options=LLMOptions(reasoning_effort="high"),
        system_prompt="system",
        memory_reflection=True,
        memory_reflection_options=None,
        enable_workspace_skills=True,
        capability_runtime_override=None,
        terminal_binding=OwnedTerminalRuntime(),
        permission_policy=default_permission_policy(),
        mcp_supervisor=McpServerSupervisor(),
        mcp_installation=empty_mcp_installation(),
        reopen_deadline_monotonic=None,
        process_owner_id="host-core:test",
        admission_generation=generation,
    )


def test_host_build_fact_excludes_live_objects_and_live_carriers_reject_serialization(
    tmp_path: Path,
) -> None:
    admission = _admission(tmp_path)

    fact_payload = asdict(admission.build_fact)
    assert fact_payload["runtime_session_id"] == "runtime:1"
    assert "settings" not in fact_payload
    assert "mcp_supervisor" not in fact_payload

    for value in (admission.live_bindings, admission):
        with pytest.raises(TypeError):
            asdict(value)
        with pytest.raises(TypeError, match="process-local"):
            pickle.dumps(value)


def test_host_live_rebinding_changes_operational_identity_not_semantic_fact(
    tmp_path: Path,
) -> None:
    first = _admission(tmp_path, generation=1)
    rebound = _admission(tmp_path, generation=2)

    assert first.build_fact == rebound.build_fact
    assert first.build_fact.fact_fingerprint == rebound.build_fact.fact_fingerprint
    assert (
        first.live_bindings.ordered_binding_identity_fingerprints
        != rebound.live_bindings.ordered_binding_identity_fingerprints
    )
    assert first.admission_fingerprint != rebound.admission_fingerprint

    with pytest.raises(ValueError, match="fact fingerprint mismatch"):
        replace(
            first.build_fact,
            terminal_binding_semantic_fingerprint="sha256:wrong",
            fact_fingerprint=first.build_fact.fact_fingerprint,
        )


def test_mcp_installation_must_match_supervisor_epoch(tmp_path: Path) -> None:
    supervisor = McpServerSupervisor()
    installation = replace(empty_mcp_installation(), config_epoch=1)
    workspace = resolve_workspace(
        HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=tmp_path,
        )
    )
    with pytest.raises(ValueError, match="MCP installation/supervisor"):
        build_host_runtime_admission(
            settings=_settings(),
            workspace=workspace,
            runtime_session_id=None,
            graph_id=None,
            model_role=ModelRole.PRO,
            options=None,
            system_prompt=None,
            memory_reflection=False,
            memory_reflection_options=None,
            enable_workspace_skills=True,
            capability_runtime_override=None,
            terminal_binding=OwnedTerminalRuntime(),
            permission_policy=default_permission_policy(),
            mcp_supervisor=supervisor,
            mcp_installation=installation,
            reopen_deadline_monotonic=None,
            process_owner_id="host-core:test",
            admission_generation=1,
        )


def test_manifest_port_preserves_workspace_domain_closed_and_limit_dimensions(
    tmp_path: Path,
) -> None:
    store = InMemorySessionManifestStore()
    policy = default_permission_policy()
    workspace_a = resolve_workspace(
        HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=tmp_path,
            memory_domain_id="domain-a",
        )
    )
    workspace_b = replace(
        workspace_a,
        memory_domain=replace(
            workspace_a.memory_domain,
            memory_domain_id="domain-b",
        ),
    )
    store.upsert_open_manifest(
        runtime_session_id="runtime:a-open",
        conversation_id="conversation:a-open",
        workspace=workspace_a,
        model_role=ModelRole.PRO,
        permission_policy=policy,
        created_by="test",
    )
    store.upsert_open_manifest(
        runtime_session_id="runtime:a-closed",
        conversation_id="conversation:a-closed",
        workspace=workspace_a,
        model_role=ModelRole.PRO,
        permission_policy=policy,
        created_by="test",
    )
    store.mark_closed("runtime:a-closed")
    store.upsert_open_manifest(
        runtime_session_id="runtime:b-open",
        conversation_id="conversation:b-open",
        workspace=workspace_b,
        model_role=ModelRole.PRO,
        permission_policy=policy,
        created_by="test",
    )

    open_a = store.list_resumable(
        workspace_root=tmp_path,
        memory_domain_id="domain-a",
        include_closed=False,
        limit=10,
    )
    assert tuple(item.runtime_session_id for item in open_a) == ("runtime:a-open",)

    all_a = store.list_resumable(
        workspace_root=tmp_path,
        memory_domain_id="domain-a",
        include_closed=True,
        limit=1,
    )
    assert len(all_a) == 1
    assert all_a[0].runtime_session_id == "runtime:a-closed"


def test_production_and_component_host_compositions_are_nominally_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    sentinel_report = object()
    monkeypatch.setattr(
        "pulsara_agent.host.core.require_production_rollout_budget_configuration",
        lambda *args, **kwargs: sentinel_report,
    )
    monkeypatch.setattr(
        "pulsara_agent.runtime.wiring.build_llm_runtime",
        lambda _config: object(),
    )

    core = HostCore.production(settings=settings)
    assert isinstance(core._composition, ProductionHostComposition)
    assert core.rollout_budget_feasibility is sentinel_report
    assert not hasattr(core, "durable")

    component = ComponentTestHostComposition()
    assert not isinstance(component, ProductionHostComposition)
    fake_lease = _TestHostProcessResourceLease(
        lease_id="test:lease",
        lease_generation=1,
        projection_service=_FakeProjectionService(),
    )
    with pytest.raises(TypeError, match="production resource lease"):
        core._composition.build_agent_runtime_wiring(
            admission=object(),  # type: ignore[arg-type]
            resources=fake_lease,
        )
