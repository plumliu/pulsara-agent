"""Bounded, local-only MCP directory pagination."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Mapping

from pulsara_agent.capability.contracts import (
    CapabilityRouteReasonCode,
    FrozenMcpRouteProjection,
    FrozenNativeToolProjectionSet,
    ToolCapabilityRouteKind,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
)
from pulsara_agent.primitives.tool_result_projection import (
    conservative_artifact_page_logical_utf8_bytes,
)

from .contracts import (
    McpCatalogSnapshot,
    McpInstallationCandidate,
    mcp_prompt_public_item,
    mcp_resource_public_item,
    mcp_resource_template_public_item,
    scope_mcp_discovery_snapshot,
)


# The self-contained cursor carries one digest of the exact cut/scope/filter,
# its offset and an HMAC.  It needs no process-local cursor registry.
_MAXIMUM_CURSOR_BYTES = 512
_MAXIMUM_PUBLIC_INSTRUCTIONS_BYTES = 8 * 1024
_ITEM_PAGE_KIND_BY_TOOL = {
    "list_mcp_resources": "RESOURCE_PAGE",
    "list_mcp_resource_templates": "RESOURCE_TEMPLATE_PAGE",
    "list_mcp_prompts": "PROMPT_PAGE",
}


@dataclass(frozen=True, slots=True)
class McpDirectoryRenderResult:
    state: str
    content: bytes


class McpDirectoryPageFactory:
    """One Host-local cursor signer; pagination never touches transport."""

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

    def render(
        self,
        *,
        arguments: Mapping[str, object],
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        catalog: McpCatalogSnapshot,
        candidates: Mapping[str, McpInstallationCandidate],
        routes: FrozenMcpRouteProjection,
        direct_projection_set: FrozenNativeToolProjectionSet,
    ) -> McpDirectoryRenderResult:
        try:
            server_filter, limit, offset = self._parse_request(
                arguments=arguments,
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
                catalog=catalog,
                routes=routes,
                direct_projection_set=direct_projection_set,
            )
        except _DirectoryTypedError as exc:
            return _error(exc.code)
        if (
            routes.joined_catalog_semantic_fingerprint
            != catalog.semantic_fingerprint
            or direct_projection_set.conversation_scope_kind is not scope_kind
            or direct_projection_set.scope_subagent_task_id
            != scope_subagent_task_id
        ):
            return _error("MCP_CATALOG_STALE")
        catalog_server_ids = {item.server_id for item in catalog.servers}
        direct_provider_names = {
            item.provider_name for item in direct_projection_set.tool_versions
        }
        if any(
            item.target.server_id not in catalog_server_ids
            or (
                item.route is ToolCapabilityRouteKind.DIRECT
                and item.version.provider_name not in direct_provider_names
            )
            for item in routes.routes
        ):
            return _error("MCP_CATALOG_STALE")
        if server_filter is None:
            rows = tuple(_server_row(item) for item in catalog.servers)
            return self._page(
                page_kind="SERVER_PAGE",
                row_key="servers",
                rows=rows,
                offset=offset,
                limit=limit,
                total=len(rows),
                base={"total_server_count": len(rows)},
                cursor_context=self._cursor_context(
                    scope_kind=scope_kind,
                    scope_subagent_task_id=scope_subagent_task_id,
                    catalog=catalog,
                    routes=routes,
                    direct_projection_set=direct_projection_set,
                    server_filter=None,
                    page_kind="SERVER_PAGE",
                    limit=limit,
                ),
            )

        server = next(
            (item for item in catalog.servers if item.server_id == server_filter),
            None,
        )
        if server is None:
            return _error("NOT_FOUND")
        candidate = candidates.get(server_filter)
        scoped_snapshot = (
            None
            if candidate is None
            else scope_mcp_discovery_snapshot(
                candidate.discovery_snapshot,
                scope_kind,
            )
        )
        if (
            candidate is None
            and server.tool_surface_semantic_fingerprint is not None
        ) or (
            scoped_snapshot is not None
            and (
                candidate.server_id != server_filter
                or scoped_snapshot.server_id != server_filter
                or scoped_snapshot.tool_surface_semantic_fingerprint
                != server.tool_surface_semantic_fingerprint
                or scoped_snapshot.catalog_semantic_fingerprint
                != server.catalog_semantic_fingerprint
            )
        ):
            return _error("MCP_CATALOG_STALE")
        semantics = () if scoped_snapshot is None else scoped_snapshot.tools
        route_by_target = {
            (item.target.server_id, item.target.remote_tool_name): item
            for item in routes.routes
        }
        direct_names = {
            item.provider_name for item in direct_projection_set.tool_versions
        }
        tool_rows = []
        for semantic in sorted(
            semantics, key=lambda item: (item.remote_tool_name, item.provider_tool_name)
        ):
            route = route_by_target.get(
                (semantic.server_id, semantic.remote_tool_name)
            )
            if route is None and semantic.provider_tool_name in direct_names:
                route_kind = ToolCapabilityRouteKind.DIRECT
                reason = CapabilityRouteReasonCode.DIRECT_NATIVE_SURFACE
            elif route is None:
                route_kind = ToolCapabilityRouteKind.UNAVAILABLE
                reason = CapabilityRouteReasonCode.SOURCE_UNAVAILABLE
            else:
                route_kind = route.route
                reason = route.public_reason_code
            tool_rows.append(
                {
                    "provider_tool_name": semantic.provider_tool_name,
                    "public_reason_code": reason.value,
                    "public_status": server.status.value,
                    "remote_tool_name": semantic.remote_tool_name,
                    "route": route_kind.value,
                    "server_id": semantic.server_id,
                }
            )
        rows = tuple(tool_rows)
        counts = {
            kind: sum(row["route"] == kind for row in rows)
            for kind in (
                ToolCapabilityRouteKind.DIRECT.value,
                ToolCapabilityRouteKind.NEW_MCP_META_ONLY.value,
                ToolCapabilityRouteKind.UNAVAILABLE.value,
            )
        }
        return self._page(
            page_kind="SERVER_TOOL_PAGE",
            row_key="tools",
            rows=rows,
            offset=offset,
            limit=limit,
            total=len(rows),
            base={
                "direct_tool_count": counts[ToolCapabilityRouteKind.DIRECT.value],
                "new_tool_count": counts[
                    ToolCapabilityRouteKind.NEW_MCP_META_ONLY.value
                ],
                "server": _server_row(server),
                "total_tool_count": len(rows),
                "unavailable_tool_count": counts[
                    ToolCapabilityRouteKind.UNAVAILABLE.value
                ],
            },
            cursor_context=self._cursor_context(
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
                catalog=catalog,
                routes=routes,
                direct_projection_set=direct_projection_set,
                server_filter=server_filter,
                page_kind="SERVER_TOOL_PAGE",
                limit=limit,
            ),
        )

    def render_items(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        catalog: McpCatalogSnapshot,
        candidates: Mapping[str, McpInstallationCandidate],
    ) -> McpDirectoryRenderResult:
        page_kind = _ITEM_PAGE_KIND_BY_TOOL.get(tool_name)
        if page_kind is None:
            raise ValueError("unknown MCP catalog list tool")
        try:
            server_filter, limit, offset = self._parse_item_request(
                arguments=arguments,
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
                catalog=catalog,
                page_kind=page_kind,
            )
        except _DirectoryTypedError as exc:
            return _error(exc.code)
        server_by_id = {item.server_id: item for item in catalog.servers}
        if server_filter is not None and server_filter not in server_by_id:
            return _error("NOT_FOUND")
        selected_server_ids = (
            (server_filter,)
            if server_filter is not None
            else tuple(sorted(server_by_id))
        )
        rows: list[dict[str, object]] = []
        for server_id in selected_server_ids:
            server = server_by_id[server_id]
            candidate = candidates.get(server_id)
            if candidate is None:
                continue
            snapshot = scope_mcp_discovery_snapshot(
                candidate.discovery_snapshot,
                scope_kind,
            )
            if (
                candidate.server_id != server_id
                or snapshot.server_id != server_id
                or snapshot.catalog_semantic_fingerprint
                != server.catalog_semantic_fingerprint
            ):
                return _error("MCP_CATALOG_STALE")
            if tool_name == "list_mcp_resources":
                rows.extend(
                    mcp_resource_public_item(server_id, item)
                    for item in snapshot.resources
                )
            elif tool_name == "list_mcp_resource_templates":
                rows.extend(
                    mcp_resource_template_public_item(server_id, item)
                    for item in snapshot.resource_templates
                )
            else:
                rows.extend(
                    mcp_prompt_public_item(server_id, item)
                    for item in snapshot.prompts
                )
        frozen_rows = tuple(rows)
        return self._page(
            page_kind=page_kind,
            row_key="items",
            rows=frozen_rows,
            offset=offset,
            limit=limit,
            total=len(frozen_rows),
            base={"total": len(frozen_rows)},
            cursor_context=self._item_cursor_context(
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
                catalog=catalog,
                server_filter=server_filter,
                page_kind=page_kind,
                limit=limit,
            ),
        )

    def _parse_request(
        self,
        *,
        arguments: Mapping[str, object],
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        catalog: McpCatalogSnapshot,
        routes: FrozenMcpRouteProjection,
        direct_projection_set: FrozenNativeToolProjectionSet,
    ) -> tuple[str | None, int, int]:
        allowed = {"server_id", "cursor", "limit"}
        if set(arguments) - allowed:
            raise _DirectoryTypedError("INVALID_ARGUMENTS")
        server = arguments.get("server_id")
        if server is not None and (not isinstance(server, str) or not server):
            raise _DirectoryTypedError("INVALID_ARGUMENTS")
        limit = arguments.get("limit", 50)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 200
        ):
            raise _DirectoryTypedError("INVALID_ARGUMENTS")
        cursor = arguments.get("cursor")
        if cursor is None:
            return server, limit, 0
        if not isinstance(cursor, str) or not cursor:
            raise _DirectoryTypedError("INVALID_ARGUMENTS")
        page_kind = "SERVER_PAGE" if server is None else "SERVER_TOOL_PAGE"
        expected = self._cursor_context(
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            catalog=catalog,
            routes=routes,
            direct_projection_set=direct_projection_set,
            server_filter=server,
            page_kind=page_kind,
            limit=limit,
        )
        payload = self._decode_cursor(cursor)
        offset = payload.pop("offset", None)
        if payload != expected or not isinstance(offset, int) or offset < 0:
            raise _DirectoryTypedError("STALE_CURSOR")
        return server, limit, offset

    def _parse_item_request(
        self,
        *,
        arguments: Mapping[str, object],
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        catalog: McpCatalogSnapshot,
        page_kind: str,
    ) -> tuple[str | None, int, int]:
        allowed = {"server_id", "cursor", "limit"}
        if set(arguments) - allowed:
            raise _DirectoryTypedError("INVALID_ARGUMENTS")
        server = arguments.get("server_id")
        if server is not None and (not isinstance(server, str) or not server):
            raise _DirectoryTypedError("INVALID_ARGUMENTS")
        limit = arguments.get("limit", 50)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 200
        ):
            raise _DirectoryTypedError("INVALID_ARGUMENTS")
        cursor = arguments.get("cursor")
        if cursor is None:
            return server, limit, 0
        if not isinstance(cursor, str) or not cursor:
            raise _DirectoryTypedError("INVALID_ARGUMENTS")
        expected = self._item_cursor_context(
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            catalog=catalog,
            server_filter=server,
            page_kind=page_kind,
            limit=limit,
        )
        payload = self._decode_cursor(cursor)
        offset = payload.pop("offset", None)
        if payload != expected or not isinstance(offset, int) or offset < 0:
            raise _DirectoryTypedError("STALE_CURSOR")
        return server, limit, offset

    @staticmethod
    def _cursor_context(
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        catalog: McpCatalogSnapshot,
        routes: FrozenMcpRouteProjection,
        direct_projection_set: FrozenNativeToolProjectionSet,
        server_filter: str | None,
        page_kind: str,
        limit: int,
    ) -> dict[str, object]:
        # The cursor is self-authenticating but it does not need to repeat three
        # full SHA-256 strings.  Bind the complete request/cut identity behind a
        # single domain-separated digest so the public descriptor's 512-byte
        # input bound and the physical decoder bound remain the same contract.
        return {
            "context_fingerprint": context_fingerprint(
                "mcp-directory-cursor-context:v1",
                {
                    "catalog": catalog.semantic_fingerprint,
                    "direct_projection": (
                        direct_projection_set.projection_set_fingerprint
                    ),
                    "limit": limit,
                    "page_kind": page_kind,
                    "routes": routes.projection_fingerprint,
                    "scope": scope_kind.value,
                    "scope_subagent_task_id": scope_subagent_task_id,
                    "server_filter": server_filter,
                },
            )
        }

    @staticmethod
    def _item_cursor_context(
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        catalog: McpCatalogSnapshot,
        server_filter: str | None,
        page_kind: str,
        limit: int,
    ) -> dict[str, object]:
        return {
            "context_fingerprint": context_fingerprint(
                "mcp-item-directory-cursor-context:v1",
                {
                    "catalog": catalog.semantic_fingerprint,
                    "limit": limit,
                    "page_kind": page_kind,
                    "scope": scope_kind.value,
                    "scope_subagent_task_id": scope_subagent_task_id,
                    "server_filter": server_filter,
                },
            )
        }

    def _page(
        self,
        *,
        page_kind: str,
        row_key: str,
        rows: tuple[dict[str, object], ...],
        offset: int,
        limit: int,
        total: int,
        base: dict[str, object],
        cursor_context: dict[str, object],
    ) -> McpDirectoryRenderResult:
        if offset > total:
            return _error("STALE_CURSOR")
        maximum = min(limit, total - offset)
        for count in range(maximum, -1, -1):
            end = offset + count
            next_cursor = (
                None
                if end >= total
                else self._encode_cursor({**cursor_context, "offset": end})
            )
            payload = {
                "next_cursor": next_cursor,
                "page_kind": page_kind,
                f"returned_{row_key[:-1]}_count": count,
                row_key: rows[offset:end],
                **base,
            }
            body = canonical_json_bytes(payload).decode("utf-8")
            quote = conservative_artifact_page_logical_utf8_bytes(
                tool_call_id="call_" + ("x" * 123),
                body=body,
                model_visible_memory_ids=(),
            )
            if quote <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES:
                if count == 0 and offset < total:
                    return _error("MCP_DIRECTORY_ROW_OVERBOUND")
                return McpDirectoryRenderResult("SUCCESS", body.encode("utf-8"))
        return _error("MCP_DIRECTORY_ROW_OVERBOUND")

    def _encode_cursor(self, payload: dict[str, object]) -> str:
        body = canonical_json_bytes(payload)
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        token = base64.urlsafe_b64encode(signature + body).decode("ascii").rstrip("=")
        if len(token.encode("ascii")) > _MAXIMUM_CURSOR_BYTES:
            raise RuntimeError("MCP directory cursor exceeded its bound")
        return token

    def _decode_cursor(self, token: str) -> dict[str, object]:
        if len(token.encode("utf-8")) > _MAXIMUM_CURSOR_BYTES:
            raise _DirectoryTypedError("INVALID_ARGUMENTS")
        try:
            raw = base64.urlsafe_b64decode(token + ("=" * (-len(token) % 4)))
        except (ValueError, UnicodeEncodeError) as exc:
            raise _DirectoryTypedError("INVALID_ARGUMENTS") from exc
        if len(raw) <= 32:
            raise _DirectoryTypedError("INVALID_ARGUMENTS")
        signature, body = raw[:32], raw[32:]
        if not hmac.compare_digest(
            signature, hmac.new(self._secret, body, hashlib.sha256).digest()
        ):
            raise _DirectoryTypedError("STALE_CURSOR")
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise _DirectoryTypedError("INVALID_ARGUMENTS") from exc
        if not isinstance(value, dict) or canonical_json_bytes(value) != body:
            raise _DirectoryTypedError("INVALID_ARGUMENTS")
        return value


class _DirectoryTypedError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _server_row(server) -> dict[str, object]:
    instructions = server.sanitized_instructions or ""
    if len(instructions.encode("utf-8")) > _MAXIMUM_PUBLIC_INSTRUCTIONS_BYTES:
        instructions = instructions.encode("utf-8")[
            :_MAXIMUM_PUBLIC_INSTRUCTIONS_BYTES
        ].decode("utf-8", "ignore")
    return {
        "bounded_public_instructions": instructions or None,
        "prompt_count": server.prompt_count,
        "public_status": server.status.value,
        "public_status_detail": server.stable_failure_category,
        "resource_count": server.resource_count,
        "resource_template_count": server.resource_template_count,
        "server_id": server.server_id,
        "tool_count": server.exposed_tool_count,
    }


def _error(code: str) -> McpDirectoryRenderResult:
    return McpDirectoryRenderResult(
        "APPLICATION_ERROR",
        canonical_json_bytes({"status": code}),
    )


__all__ = ["McpDirectoryPageFactory", "McpDirectoryRenderResult"]
