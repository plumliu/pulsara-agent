"""Local scoped MCP query projections. Pagination belongs to the unified query."""

from pulsara_agent.capability.contracts import ToolCapabilityRouteKind
from pulsara_agent.capability.source_query import minimal_row
from .contracts import (
    scope_mcp_discovery_snapshot,
    mcp_resource_public_item,
    mcp_resource_template_public_item,
    mcp_prompt_public_item,
)


def runtime_rows(runtime, plan, scope):
    if runtime is None or plan is None:
        return (
            [],
            {},
            [
                {
                    "code": "MCP_CATALOG_UNAVAILABLE",
                    "message": "No MCP runtime catalog is installed for this caller.",
                }
            ],
        )
    catalog = runtime.catalog_for_scope(scope)
    if (
        catalog.semantic_fingerprint
        != plan.mcp_catalog_route_projection.joined_catalog_semantic_fingerprint
    ):
        return (
            [],
            {},
            [
                {
                    "code": "MCP_CATALOG_STALE",
                    "message": "The borrowed catalog and route do not join.",
                }
            ],
        )
    rows, details = [], {}
    for server in catalog.servers:
        target = {"kind": "MCP_SERVER", "runtime_server_id": server.server_id}
        status = f"连接状态：{server.status.value}。"
        if server.stable_failure_category:
            status += f" {server.stable_failure_category}。"
        candidate = runtime.candidates.get(server.server_id)
        if candidate is None:
            status += "远端目录尚未发现。"
        rows.append(
            minimal_row("MCP_SERVER", server.display_name, target, None, status)
        )
        detail = {
            "target": target,
            "name": server.display_name,
            "source": None,
            "connection_status": server.status.value,
            "catalog_discovered": candidate is not None,
            "diagnostics": [],
        }
        if server.stable_failure_category:
            detail["diagnostics"].append(
                {"code": server.stable_failure_category, "message": status}
            )
        if server.sanitized_instructions:
            detail["instructions"] = server.sanitized_instructions
        details[("MCP_SERVER", server.server_id)] = detail
        snapshot = (
            scope_mcp_discovery_snapshot(candidate.discovery_snapshot, scope)
            if candidate is not None
            else None
        )
        for route in plan.mcp_catalog_route_projection.routes:
            if route.target.server_id != server.server_id:
                continue
            tool = next(
                (
                    item
                    for item in (snapshot.tools if snapshot is not None else ())
                    if item.provider_tool_name == route.version.provider_name
                ),
                None,
            )
            target = {
                "kind": "MCP_TOOL",
                "server_id": server.server_id,
                "tool_name": route.version.provider_name,
            }
            if route.route is ToolCapabilityRouteKind.DIRECT:
                status = "当前直接调用面中的工具；详情提供该调用面的参数定义。"
            elif route.route is ToolCapabilityRouteKind.NEW_MCP_META_ONLY:
                status = "需先查看详情，再用返回的 tool_ref 调用。"
            else:
                status = f"当前不可调用：{route.public_reason_code.value}。"
            rows.append(
                minimal_row(
                    "MCP_TOOL",
                    route.version.provider_name,
                    target,
                    None,
                    status,
                    None if tool is None else tool.description,
                )
            )
        if snapshot is None:
            continue
        for kind, items, identity_field, public in (
            ("MCP_RESOURCE", snapshot.resources, "uri", mcp_resource_public_item),
            (
                "MCP_RESOURCE_TEMPLATE",
                snapshot.resource_templates,
                "uri_template",
                mcp_resource_template_public_item,
            ),
            ("MCP_PROMPT", snapshot.prompts, "name", mcp_prompt_public_item),
        ):
            for item in items:
                identity = getattr(item, identity_field)
                target = {
                    "kind": kind,
                    "server_id": server.server_id,
                    identity_field: identity,
                }
                rows.append(
                    minimal_row(
                        kind,
                        item.name,
                        target,
                        None,
                        "已发现元数据；尚未读取或渲染。",
                        item.description,
                    )
                )
                details[(kind, server.server_id, identity)] = {
                    "target": target,
                    "source": None,
                    **public(server.server_id, item),
                }
    return rows, details, []


def frozen_runtime_config(runtime, server_id):
    """ROOT-only caller adaptation; read the exact candidate's existing slot config.

    No supervisor latest-config lookup, secret resolution, transport I/O or new
    snapshot. Equality to the candidate's frozen config identities is required.
    """
    candidate = runtime.candidates.get(server_id) if runtime is not None else None
    if candidate is None:
        return None
    config = candidate.slot_lease._slot.client.config
    if (
        config.semantic_config_fingerprint
        != candidate.expected_semantic_config_fingerprint
        or config.runtime_config_fingerprint
        != candidate.expected_runtime_config_fingerprint
        or config.resolved_config_identity
        != candidate.expected_resolved_config_identity
    ):
        return None
    return config
