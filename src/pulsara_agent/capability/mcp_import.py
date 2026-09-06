"""One-shot external MCP configuration import, never a runtime config dialect.

Unclassified values stay in the caller's private draft. Only explicit user
classification can make a literal header/environment value public.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Literal, Mapping

from pulsara_agent.mcp_config import MAXIMUM_MCP_CONFIG_BYTES
from pulsara_agent.mcp_credentials import (
    BoundSecretValue,
    EnvironmentVariableReference,
    ManagedLocalCredentialReference,
    McpCredentialBinding,
    McpCredentialOwner,
    secret_to_dict,
)
from pulsara_agent.conversation_kernel.mcp.wire import (
    DEFAULT_MCP_WIRE_BOUNDS,
    bounded_json_loads,
)
from .mcp_management import McpSecretMutation


@dataclass(frozen=True, slots=True)
class ImportVariable:
    name: str
    default: str | None = None
    environment: bool = False
    file_path: str | None = None


@dataclass(frozen=True, slots=True)
class McpImportIssue:
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class McpImportDraft:
    server_id: str
    config: dict[str, object] = field(repr=False)
    inputs: tuple[tuple[str, str], ...] = field(repr=False)
    private_targets: frozenset[str] = frozenset()
    issues: tuple[McpImportIssue, ...] = ()
    notices: tuple[str, ...] = ()

    def preview(self) -> dict[str, object]:
        """No source literals, including malformed credentials, escape preview."""
        fields = []
        for target, value in self.inputs:
            try:
                variables = parse_import_template(value)
            except ValueError:
                variables = ()
            fields.append(
                {
                    "target": target,
                    "private": target in self.private_targets,
                    "template_literals": any(
                        isinstance(part, str) for part in variables
                    )
                    and any(
                        isinstance(part, ImportVariable) and part.environment
                        for part in variables
                    ),
                    "variables": [
                        {
                            "name": part.name,
                            "has_default": part.default is not None,
                            "environment": part.environment,
                            "file_path": part.file_path,
                        }
                        for part in variables
                        if isinstance(part, ImportVariable)
                    ],
                }
            )
        return {
            "server_id": self.server_id,
            "transport": self.config.get("transport", {}).get("type"),
            "fields": fields,
            "issues": [
                {"path": issue.path, "message": issue.message} for issue in self.issues
            ],
            "notices": list(self.notices),
        }


_VARIABLE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^{}]*))?\}|\{env:([A-Za-z_][A-Za-z0-9_]*)\}|\{file:([^{}\x00]+)\}"
)


def parse_import_template(value: str) -> tuple[str | ImportVariable, ...]:
    parts: list[str | ImportVariable] = []
    start = 0
    for match in _VARIABLE.finditer(value):
        literal = value[start : match.start()]
        if "${" in literal or "{env:" in literal or "{file:" in literal:
            raise ValueError("模板不能嵌套；文件引用需要用户单独选择读取。")
        if literal:
            parts.append(literal)
        parts.append(ImportVariable(
            match[1] or match[3] or "file:" + match[4], match[2], bool(match[3]), match[4],
        ))
        start = match.end()
    tail = value[start:]
    if "${" in tail or "{env:" in tail or "{file:" in tail:
        raise ValueError("模板不能嵌套；文件引用需要用户单独选择读取。")
    if tail:
        parts.append(tail)
    return tuple(parts)


def _jsonc(text: str) -> object:
    if len(text.encode("utf-8")) > MAXIMUM_MCP_CONFIG_BYTES:
        raise ValueError("MCP 导入超过已有配置字节上限。")
    # Remove comments only outside strings, preserving token separation/newlines.
    output = list(text)
    index, quoted = 0, False
    while index < len(text):
        char = text[index]
        if quoted:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif text.startswith("//", index):
            end = text.find("\n", index)
            end = len(text) if end == -1 else end
            output[index:end] = " " * (end - index)
            index = end
            continue
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                raise ValueError("MCP JSONC 注释未结束。")
            end += 2
            output[index:end] = [
                "\n" if value == "\n" else " " for value in text[index:end]
            ]
            index = end
            continue
        index += 1
    # JSONC trailing commas, never comma-like content inside a string.
    normalized = "".join(output)
    index, quoted = 0, False
    while index < len(normalized):
        char = normalized[index]
        if quoted:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == ",":
            following = index + 1
            while following < len(normalized) and normalized[following].isspace():
                following += 1
            if following < len(normalized) and normalized[following] in "}]":
                output[index] = " "
        index += 1
    normalized = "".join(output)
    bounds = DEFAULT_MCP_WIRE_BOUNDS
    try:
        bounded_json_loads(
            normalized.encode(),
            maximum_bytes=MAXIMUM_MCP_CONFIG_BYTES,
            maximum_nodes=bounds.maximum_wire_json_nodes,
            maximum_depth=bounds.maximum_wire_json_depth,
        )

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        return json.loads(normalized, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("MCP JSON/JSONC 格式无效或包含重复字段。") from None


def read_mcp_import(
    text: str,
    *,
    shape: Literal["auto", "mcpServers", "opencode", "map", "server"] = "auto",
    server_id: str | None = None,
) -> tuple[McpImportDraft, ...]:
    raw = _jsonc(text)
    if not isinstance(raw, dict):
        raise ValueError("MCP 导入根必须是对象。")
    if shape == "auto":
        candidates = []
        if "mcpServers" in raw:
            candidates.append("mcpServers")
        if "mcp" in raw:
            candidates.append("opencode")
        if "command" in raw or "url" in raw:
            candidates.append("server")
        if not candidates:
            candidates.append("map")
        if len(candidates) != 1:
            raise ValueError("配置含多种 MCP 形状，请明确选择格式。")
        shape = candidates[0]
    if shape not in {"mcpServers", "opencode", "map", "server"}:
        raise ValueError("请选择支持的 MCP 导入格式。")
    if shape in {"mcpServers", "opencode"}:
        selected = raw.get("mcpServers" if shape == "mcpServers" else "mcp")
    elif shape == "server":
        if not server_id:
            raise ValueError("导入单个 MCP 时请填写服务 ID。")
        selected = {server_id: raw}
    else:
        selected = raw
    if not isinstance(selected, dict):
        raise ValueError("所选 MCP 列表必须是对象。")
    return tuple(
        _draft(key, value, opencode=shape == "opencode")
        for key, value in selected.items()
    )


def _draft(server_id: str, raw: object, *, opencode: bool) -> McpImportDraft:
    if not isinstance(raw, dict):
        return McpImportDraft(
            server_id,
            {},
            (),
            issues=(McpImportIssue(server_id, "服务定义必须是对象。"),),
        )
    issues, notices = [], []
    allowed = {
        "type",
        "command",
        "args",
        "cwd",
        "env",
        "environment",
        "url",
        "headers",
        "oauth",
        "bearer_token_env_var",
        "enabled",
        "disabled",
        "description",
        "name",
        "timeout",
        "auth",
    }
    for key in raw.keys() - allowed:
        issues.append(
            McpImportIssue(key, "此行为字段没有原生等价项；请明确处理后再导入。")
        )
    inputs: dict[str, str] = {}
    private = set()
    kind = raw.get("type")
    if kind in {"stdio", "local"} or kind is None and "command" in raw:
        command = raw.get("command")
        args = raw.get("args", [])
        if isinstance(command, list):
            if (
                not opencode
                or not command
                or not all(isinstance(item, str) for item in command)
                or "args" in raw
            ):
                issues.append(
                    McpImportIssue(
                        "command", "命令数组只适用于 OpenCode；不得与 args 同时定义。"
                    )
                )
                command, args = "", []
            else:
                command, args = command[0], command[1:]
        transport = {
            "type": "stdio",
            "command": command,
            "args": args,
            "cwd": raw.get("cwd", "."),
            "env": {},
            "secret_env": {},
        }
        env = raw.get("env", raw.get("environment", {}))
        if "env" in raw and "environment" in raw:
            issues.append(McpImportIssue("env", "env 与 environment 重复定义。"))
        if not isinstance(env, dict) or any(
            not isinstance(v, str) for v in env.values()
        ):
            issues.append(McpImportIssue("env", "环境变量必须是文字值。"))
        else:
            inputs.update(("env:" + key, value) for key, value in env.items())
        if raw.get("headers") or raw.get("oauth") or raw.get("bearer_token_env_var"):
            issues.append(McpImportIssue("auth", "stdio 连接不使用 HTTP 认证。"))
    elif kind in {None, "http", "streamable-http", "streamable_http", "remote", "sse"}:
        transport = {
            "type": "sse" if kind == "sse" else "streamable_http",
            "endpoint": "",
        }
        endpoint = raw.get("url", "")
        if isinstance(endpoint, str):
            inputs["endpoint"] = endpoint
        else:
            issues.append(McpImportIssue("url", "服务地址必须是文字。"))
        if kind in {None, "remote"}:
            notices.append(
                "已推荐 Streamable HTTP；如果来源明确使用 SSE，请在保存前选择 SSE。"
            )
        if "command" in raw or "env" in raw or "environment" in raw:
            issues.append(
                McpImportIssue("transport", "远程地址与本地命令配置不能混合。")
            )
    else:
        transport = {}
        issues.append(McpImportIssue("type", "不支持此连接方式。"))
    config: dict[str, object] = {
        "transport": transport,
        "auth": {"type": "none"},
        "public_headers": {},
        "enabled": raw.get("enabled", not raw.get("disabled", False)),
        "display_name": raw.get("name", server_id),
    }
    if "enabled" in raw and "disabled" in raw:
        issues.append(McpImportIssue("enabled", "enabled 与 disabled 重复定义。"))
    for field_name in ("enabled", "disabled"):
        if field_name in raw and type(raw[field_name]) is not bool:
            issues.append(McpImportIssue(field_name, "启用状态必须是布尔值。"))
    if "timeout" in raw:
        if opencode:
            config["default_tool_timeout_ms"] = raw["timeout"]
        else:
            issues.append(
                McpImportIssue(
                    "timeout", "来源未明确超时单位，请在编辑器中填写毫秒值。"
                )
            )
    headers = raw.get("headers", {})
    if not isinstance(headers, dict) or any(
        not isinstance(v, str) for v in headers.values()
    ):
        issues.append(McpImportIssue("headers", "Header 必须是文字值。"))
    else:
        inputs.update(("header:" + key, value) for key, value in headers.items())
        private.update(
            "header:" + key for key in headers if key.lower() == "authorization"
        )
    if "bearer_token_env_var" in raw:
        try:
            reference = EnvironmentVariableReference(raw["bearer_token_env_var"])
            config["auth"] = {"type": "bearer", "reference": secret_to_dict(reference)}
        except (TypeError, ValueError):
            issues.append(McpImportIssue("bearer_token_env_var", "环境变量引用无效。"))
    oauth = raw.get("oauth")
    if oauth is not None and oauth is not False:
        if oauth is True:
            oauth = {}
        if not isinstance(oauth, dict):
            issues.append(McpImportIssue("oauth", "OAuth 配置必须是对象。"))
        else:
            auth = {"type": "oauth"}
            aliases = {
                "clientId": "client_id",
                "client_id": "client_id",
                "CLIENT_ID": "client_id",
                "clientSecret": "client_secret",
                "client_secret": "client_secret",
                "CLIENT_SECRET": "client_secret",
                "scope": "scope",
                "redirectUri": "redirect_uri",
                "redirect_uri": "redirect_uri",
                "resource": "resource",
                "client_metadata_url": "client_metadata_url",
            }
            for key, value in oauth.items():
                target = aliases.get(key)
                if target is None:
                    issues.append(
                        McpImportIssue("oauth." + key, "OAuth 字段没有原生等价项。")
                    )
                elif "oauth:" + target in inputs:
                    issues.append(
                        McpImportIssue("oauth." + key, "OAuth 字段重复定义。")
                    )
                elif not isinstance(value, str):
                    issues.append(
                        McpImportIssue("oauth." + key, "OAuth 字段必须是文字值。")
                    )
                else:
                    inputs["oauth:" + target] = value
                    if target == "client_secret":
                        private.add("oauth:" + target)
            if config["auth"]["type"] != "none" or any(
                t.startswith("header:") for t in private
            ):
                issues.append(McpImportIssue("auth", "OAuth 与其他认证重复定义。"))
            config["auth"] = auth
    if "auth" in raw:
        issues.append(
            McpImportIssue("auth", "请选择明确的 bearer、Header 或 OAuth 导入字段。")
        )
    for target, value in inputs.items():
        try:
            parse_import_template(value)
        except ValueError as exc:
            issues.append(McpImportIssue(target, str(exc)))
    return McpImportDraft(
        server_id,
        config,
        tuple(inputs.items()),
        frozenset(private),
        tuple(issues),
        tuple(notices),
    )


def materialize_mcp_import(
    draft: McpImportDraft,
    owner: McpCredentialOwner,
    *,
    classifications: Mapping[str, str],
    values: Mapping[str, str],
    transport: str | None = None,
    allow_http_localhost: bool = False,
) -> tuple[dict[str, object], tuple[McpSecretMutation, ...]]:
    if draft.issues:
        raise ValueError("请先处理预览中标出的配置字段。")
    if owner.server_id != draft.server_id:
        raise ValueError("导入目标与所选服务不一致。")
    config = json.loads(json.dumps(draft.config))
    if type(allow_http_localhost) is not bool:
        raise ValueError("本机 HTTP 选择必须为布尔值。")
    if allow_http_localhost:
        if config["transport"].get("type") == "stdio":
            raise ValueError("本地命令不使用 HTTP 网络选择。")
        config["transport"]["allow_http_localhost"] = True
    secrets = []
    secret_headers = {}
    if transport is not None:
        if (
            transport not in {"sse", "streamable_http"}
            or config["transport"].get("type") == "stdio"
        ):
            raise ValueError("只能明确选择远程 HTTP 或 SSE 连接。")
        config["transport"]["type"] = transport
    targets = {key for key, _ in draft.inputs}
    if set(classifications) - targets - {key + ":template_literals" for key in targets}:
        raise ValueError("存在不属于此配置的输入分类。")
    for target, value in draft.inputs:
        forced_private = target in draft.private_targets
        category = "private" if forced_private else classifications.get(target)
        if target == "endpoint" or target.startswith("oauth:") and not forced_private:
            category = "public"
        if (
            category not in {"public", "private"}
            or forced_private
            and classifications.get(target, "private") != "private"
        ):
            raise ValueError("请确认每个 Header/环境变量是普通值还是私有值。")
        binding_name = (
            "oauth-client-secret" if target == "oauth:client_secret" else target
        )
        parts = parse_import_template(value)
        lowered = []
        for part in parts:
            if isinstance(part, str):
                lowered.append(part)
            elif category == "private" and part.environment and part.name not in values:
                lowered.append(EnvironmentVariableReference(part.name))
            else:
                supplied = values.get(part.name, part.default)
                if supplied is None:
                    raise ValueError(f"请填写输入：{part.name}。")
                if not isinstance(supplied, str):
                    raise ValueError("导入输入必须是文字。")
                lowered.append(supplied)
        if category == "private":
            if any(isinstance(part, EnvironmentVariableReference) for part in lowered):
                if (
                    any(isinstance(part, str) and part for part in lowered)
                    and classifications.get(target + ":template_literals") != "public"
                ):
                    raise ValueError(
                        "请确认环境引用模板中的常量部分不包含秘密，或填写全部变量以私有值保存。"
                    )
                # Preserve explicit env references; non-environment user values
                # cannot become secret literals inside a public template.
                if any(
                    isinstance(part, ImportVariable)
                    and (not part.environment or part.name in values)
                    for part in parts
                ):
                    raise ValueError("混合私有输入请明确填写全部变量后保存。")
                secret = (
                    lowered[0]
                    if len(lowered) == 1
                    else BoundSecretValue(tuple(lowered))
                )
            else:
                binding = McpCredentialBinding(owner, binding_name)
                secrets.append(McpSecretMutation(binding, "".join(lowered)))
                secret = ManagedLocalCredentialReference(binding)
            encoded = secret_to_dict(secret)
            if target.startswith("env:"):
                config["transport"]["secret_env"][target[4:]] = encoded
            elif target.startswith("header:"):
                secret_headers[target[7:]] = encoded
            else:
                config["auth"]["client_secret"] = encoded
        else:
            literal = "".join(lowered)
            if target == "endpoint":
                config["transport"]["endpoint"] = literal
            elif target.startswith("env:"):
                config["transport"]["env"][target[4:]] = literal
            elif target.startswith("header:"):
                config["public_headers"][target[7:]] = literal
            elif target.startswith("oauth:"):
                config["auth"][target[6:]] = literal
    if secret_headers:
        if config["auth"]["type"] != "none":
            raise ValueError("私有 Header 与另一种认证重复，请选择完整的认证方式。")
        config["auth"] = {"type": "static_headers", "headers": secret_headers}
    return config, tuple(secrets)
