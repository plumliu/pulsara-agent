from __future__ import annotations

import builtins

import pytest

from pulsara_agent.runtime.permission import (
    PermissionMode,
    parse_permission_mode,
    preset_to_policy,
)
from tests.support import test_resolved_target_fact


def run_start_permission_fields(
    run_id: str,
    *,
    mode: str | PermissionMode = PermissionMode.BYPASS_PERMISSIONS,
    source: str = "session_default",
) -> dict[str, object]:
    parsed = parse_permission_mode(mode)
    return {
        "permission_snapshot_id": f"permission_snapshot:{run_id}",
        "permission_mode": parsed.value,
        "permission_policy": preset_to_policy(parsed).to_dict(),
        "permission_snapshot_source": source,
        "model_target": test_resolved_target_fact(),
    }


builtins.run_start_permission_fields = run_start_permission_fields


@pytest.fixture(autouse=True)
def _isolate_user_mcp_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ordinary tests hermetic from ~/.pulsara/mcp.yaml.

    HostCore and HostSession intentionally load user-level MCP servers in
    production.  Unit tests should not inherit the developer's personal MCP
    config: a remote user MCP can make tests slow, flaky, or timing-dependent.
    MCP-specific tests can still override these patched symbols explicitly with
    their own monkeypatches.
    """

    def _empty_configs(*, workspace_root):
        return ()

    monkeypatch.setattr(
        "pulsara_agent.host.core.load_mcp_server_configs", _empty_configs
    )
    monkeypatch.setattr(
        "pulsara_agent.host.session.load_mcp_server_configs", _empty_configs
    )
