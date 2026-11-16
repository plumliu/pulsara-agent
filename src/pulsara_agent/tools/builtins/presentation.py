"""Normalize advisory display requests; view owners perform actual reads/navigation."""
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
import re
from urllib.parse import urlsplit

from pulsara_agent.code_mode.contracts import CodeModeError
from pulsara_agent.message import ToolResultState
from pulsara_agent.identity import require_id
from pulsara_agent.conversation_kernel.presentation_targets import PresentationReadUnavailable, ResolvedPresentationObject
from pulsara_agent.ports.tool_execution import ToolCall, ToolBusinessValue, ToolExecutionResult, freeze_tool_json_object
from pulsara_agent.conversation_kernel.content_parts import ResultPart
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
        kind = {"PATH": "file", "URL": "browser", "AGENT": "agent"}.get(target.get("kind"))
        fields = {"file": "path", "browser": "url", "agent": "agent_id"}
        if not isinstance(kind, str) or kind not in fields:
            raise ValueError("不支持的目标类型")
        field = fields[kind]
        if set(target) != {tag, field}:
            raise ValueError(f"该目标只接受 {tag} 和 {field}")
        value = target[field]
        if not isinstance(value, str) or not value.strip() or '\x00' in value:
            raise ValueError(f"{field} 必须是非空文本")
        if kind == "agent":
            require_id(value, "session")
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
        public = {"target": {"kind": target["kind"], field: result[field]}}
        if kind == "agent":
            if not self.root_session_id:
                raise ValueError("ROOT context is unavailable")
            public["root_session_id"] = require_id(self.root_session_id, "session")
        return public

    async def execute_async(self, call: ToolCall, *, resolve_object: Callable[[str, str], Awaitable[ResolvedPresentationObject]]) -> ToolExecutionResult:
        arguments = call.arguments
        if set(arguments) != {"targets"} or not isinstance(arguments["targets"], (list, tuple)) or not arguments["targets"]:
            raise ValueError("open_in_pulsara 只接受非空 targets 数组。")
        targets, accepted_indexes, errors = [], [], []
        for index, target in enumerate(arguments["targets"]):
            try:
                normalized = self.normalize_target(target)
                value = normalized["target"]
                if value["kind"] == "AGENT":
                    identity = value["agent_id"]
                    resolved = await resolve_object("AGENT", identity)
                    if (resolved.kind != "AGENT" or resolved.target_id != identity
                            or resolved.root_session_id != self.root_session_id):
                        raise ValueError("对象不属于当前 ROOT 会话。")
                targets.append(freeze_tool_json_object(normalized))
                accepted_indexes.append(index)
            except (ValueError, LookupError, CodeModeError, PresentationReadUnavailable, TimeoutError, ConnectionError) as error:
                errors.append((index, str(error)))
        if not errors:
            explanation = "已提交展示请求。"
        else:
            explanation = ("已接受 " + "、".join(f"targets[{index}]" for index in accepted_indexes) + " 的展示请求。"
                           if accepted_indexes else "没有展示请求被接受。")
            explanation += "".join(f"\ntargets[{index}] 未接受：{message}" for index, message in errors)
        return ToolExecutionResult(
            call_id=call.id, tool_name=call.name,
            status=ToolResultState.ERROR if errors else ToolResultState.SUCCESS,
            business_value=ToolBusinessValue(content=(ResultPart("text", explanation),)),
            result_facts={"presentation_targets": tuple(targets)} if targets else {},
        )
