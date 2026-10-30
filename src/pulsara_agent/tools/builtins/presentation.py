"""Normalize advisory display requests; view owners perform actual reads/navigation."""
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
import json
import re
from urllib.parse import urlsplit

from pulsara_agent.code_mode.contracts import CodeModeError
from pulsara_agent.message import ToolResultState
from pulsara_agent.identity import require_id
from pulsara_agent.conversation_kernel.presentation_targets import PresentationReadUnavailable, ResolvedPresentationObject
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.workspace import WorkspaceTool



@dataclass
class OpenInPulsaraTool(WorkspaceTool):
    host_session_id: str = ""
    root_session_id: str = ""
    name: str = "open_in_pulsara"

    def normalize_target(self, target: object) -> dict:
        if not isinstance(target, dict):
            raise ValueError("目标必须是对象")
        if "kind" in target and not isinstance(target["kind"], str):
            raise ValueError("不支持的目标类型")
        tag = "kind"
        kind = {"PATH": "file", "URL": "browser", "PROCESS": "terminal", "MCP_SERVER": "mcp", "PANEL": "panel", "AGENT": "agent", "CELL": "cell"}.get(target.get("kind"))
        fields = {"file": "path", "browser": "url", "terminal": "process_id", "mcp": "runtime_server_id", "panel": "name", "agent": "agent_id", "cell": "cell_id"}
        if not isinstance(kind, str) or kind not in fields:
            raise ValueError("不支持的目标类型")
        field = fields[kind]
        if set(target) != {tag, field}:
            raise ValueError(f"该目标只接受 {tag} 和 {field}")
        value = target[field]
        if not isinstance(value, str) or not value.strip() or '\x00' in value:
            raise ValueError(f"{field} 必须是非空文本")
        value = value.strip()
        result = {"type": kind, field: value}
        if kind == "file":
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value):
                raise ValueError("文件目标需要本地路径")
            result[field] = str(self._read_access_path(value))
        elif kind == "browser":
            try:
                url = urlsplit(value)
                if url.scheme.lower() not in {"http", "https"}:
                    raise ValueError("网页地址只支持 HTTP(S)")
                if not re.match(r"^https?://", value, re.I) or not url.hostname or any(c.isspace() for c in url.netloc) or '\\' in value:
                    raise ValueError("请输入完整的 HTTP(S) 地址")
                if url.username is not None or url.password is not None:
                    raise ValueError("网页地址不能包含用户名或密码")
                _ = url.port
            except ValueError:
                # Never echo a supplied URL: rejected input may contain credentials.
                raise ValueError("请输入不含凭据的完整 HTTP(S) 地址") from None
        elif kind == "panel":
            if value not in {"capabilities", "background", "subagents", "cells", "browser"}:
                raise ValueError("入口名称只支持 capabilities、background、subagents、cells、browser")
        elif kind in {"agent", "cell"}:
            require_id(value, "session" if kind == "agent" else "cell")
        else:
            result["host_session_id"] = self.host_session_id
        public = {"target": {"kind": target["kind"], field: result[field]}}
        if "host_session_id" in result:
            public["host_session_id"] = result["host_session_id"]
        if kind in {"agent", "cell"} or (kind == "panel" and value in {"subagents", "cells"}):
            if not self.root_session_id:
                raise ValueError("ROOT context is unavailable")
            public["root_session_id"] = self.root_session_id
        if kind == "cell" or (kind == "panel" and value == "cells"):
            if not self.host_session_id:
                raise ValueError("Host context is unavailable")
            public["host_session_id"] = self.host_session_id
        return public

    async def execute_async(self, call: ToolCall, *, resolve_object: Callable[[str, str], Awaitable[ResolvedPresentationObject]]) -> ToolExecutionResult:
        targets, errors = [], []
        for index, target in enumerate(call.arguments["targets"]):
            try:
                normalized = self.normalize_target(target)
                value = normalized["target"]
                if value["kind"] in {"AGENT", "CELL"}:
                    kind = value["kind"]
                    identity = value["agent_id" if kind == "AGENT" else "cell_id"]
                    resolved = await resolve_object(kind, identity)
                    if (resolved.kind != kind or resolved.target_id != identity
                            or resolved.root_session_id != self.root_session_id
                            or (kind == "CELL" and resolved.host_session_id != self.host_session_id)):
                        raise ValueError("Object owner does not match the calling context")
                targets.append(normalized)
            except (ValueError, LookupError, CodeModeError, PresentationReadUnavailable, TimeoutError, ConnectionError) as error:
                errors.append({"index": index, "message": str(error)})
        body = {"targets": targets}
        if errors:
            body["errors"] = errors
        return self._result(call, status=ToolResultState.SUCCESS, output=json.dumps(body, ensure_ascii=False))
