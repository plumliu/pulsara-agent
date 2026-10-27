"""Normalize advisory display requests; view owners perform actual reads/navigation."""
from dataclasses import dataclass
import json
import re
from urllib.parse import urlsplit

from pulsara_agent.message import ToolResultState
from pulsara_agent.identity import is_valid_id
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.workspace import WorkspaceTool



@dataclass
class OpenInPulsaraTool(WorkspaceTool):
    host_session_id: str = ""
    name: str = "open_in_pulsara"

    def normalize_target(self, target: object) -> dict:
        if not isinstance(target, dict):
            raise ValueError("目标必须是对象")
        if "kind" in target and not isinstance(target["kind"], str):
            raise ValueError("不支持的目标类型")
        task = target.get("type") == "task"
        tag = "type" if task else "kind"
        kind = "task" if task else {"PATH": "file", "URL": "browser", "PROCESS": "terminal", "MCP_SERVER": "mcp", "PANEL": "panel"}.get(target.get("kind"))
        fields = {"file": "path", "browser": "url", "task": "task_id", "terminal": "process_id", "mcp": "runtime_server_id", "panel": "name"}
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
        elif kind == "task":
            if not is_valid_id(value, "task"):
                raise ValueError("task_id 必须来自真实子代理任务")
        elif kind == "panel":
            if value not in {"capabilities", "tasks", "background", "browser"}:
                raise ValueError("入口名称只支持 capabilities、tasks、background、browser")
        else:
            result["host_session_id"] = self.host_session_id
        if task:
            return result
        public = {"target": {"kind": target["kind"], field: result[field]}}
        if "host_session_id" in result:
            public["host_session_id"] = result["host_session_id"]
        return public

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        targets, errors = [], []
        for index, target in enumerate(call.arguments["targets"]):
            try:
                targets.append(self.normalize_target(target))
            except ValueError as error:
                errors.append({"index": index, "message": str(error)})
        body = {"targets": targets}
        if errors:
            body["errors"] = errors
        return self._result(call, status=ToolResultState.SUCCESS, output=json.dumps(body, ensure_ascii=False))
