"""One-shot external manifest conversion. Runtime only sees native packages.

The selected source remains held and revalidated through publication. No import
registry, executable template language, service-specific adapter or source edit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from pulsara_agent.capability.mcp_import import (
    _jsonc,
    read_mcp_import,
    parse_import_template,
    ImportVariable,
)
from .package_core import (
    _open_source_root,
    _observe_membership,
    _read_bounded_entry,
    _revalidate_source,
    _open_regular_entry,
    _entry_matches,
    _check_abort,
    MAXIMUM_PLUGIN_JSON_BYTES,
    COPY_CHUNK_BYTES,
    PackageEntryKind,
    PluginPackageRaced,
    PluginSourceObserver,
    PluginPackageInvalid,
)

SOURCE_MANIFESTS = {
    "native": "plugin.json",
    "claude": ".claude-plugin/plugin.json",
    "codex": ".codex-plugin/plugin.json",
    "cursor": ".cursor-plugin/plugin.json",
}


def preview_plugin_imports(
    source, *, deadline_monotonic, cancellation, credential_boundary
):
    """Discover release manifests only; each preview uses its existing parser.

    This is disposable UI information, not install authority. Installation still
    observes and validates the explicitly selected distribution independently.
    """
    descriptor = _open_source_root(source)
    try:
        entries = _observe_membership(
            descriptor, deadline_monotonic=deadline_monotonic, cancellation=cancellation
        )
        paths = {
            entry.relative_path
            for entry in entries
            if entry.kind is PackageEntryKind.REGULAR_FILE
        }
        formats = [
            name
            for name, path in SOURCE_MANIFESTS.items()
            if PurePosixPath(path) in paths
        ]
    finally:
        os.close(descriptor)
    if not formats:
        raise PluginImportError(
            "目录中未找到支持的插件 manifest。请选择包含 plugin.json 或 .claude-plugin / .codex-plugin / .cursor-plugin/plugin.json 的插件根目录。"
        )
    candidates = []
    for source_format in formats:
        preview, error = None, None
        try:
            if source_format == "native":
                scrub = credential_boundary.capture_scrub_set(
                    deadline_monotonic=deadline_monotonic, cancellation=cancellation
                )
                with PluginSourceObserver(credential_boundary).observe(
                    source,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=cancellation,
                    scrub_set=scrub,
                ) as observed:
                    summary = observed.summary
                    preview = {
                        "name": summary.manifest.name,
                        "source_format": source_format,
                        "skills": [item.name for item in summary.skills.skills],
                        "hooks": sorted(
                            {item.event for item in summary.hooks.hook_definitions}
                        ),
                        "mcp": [
                            {
                                "server_id": item.local_server_id,
                                "transport": item.kind.value.replace("-", "_"),
                                "fields": [],
                                "issues": [],
                                "notices": [],
                            }
                            for item in summary.mcp.mcp_servers
                        ],
                        "notices": (
                            ["部分组件未通过原生校验；安装时会报告具体组件问题。"]
                            if observed.diagnostics
                            else []
                        ),
                    }
            else:
                imported = PluginSourceImport(
                    source,
                    source_format,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=cancellation,
                )
                try:
                    preview = imported.preview()
                    imported.revalidate()
                finally:
                    imported.close()
        except PluginImportError as exc:
            error = str(exc)
        except (PluginPackageInvalid, ValueError):
            error = "无法解析此发行版，请检查 manifest 与组件定义。"
        candidates.append(
            {
                "source_format": source_format,
                "manifest": SOURCE_MANIFESTS[source_format],
                "preview": preview,
                "error": error,
            }
        )
    return {"candidates": candidates}


def observe_plugin_import(observer, request, *, scrub_set):
    if request.source_format == "native":
        if request.import_classifications or request.import_public_values:
            raise PluginImportError("原生插件不使用外部格式导入参数。")
        return observer.observe(
            request.source_path,
            deadline_monotonic=request.deadline_monotonic,
            cancellation=request.cancellation,
            scrub_set=scrub_set,
        )
    source = PluginSourceImport(
        request.source_path,
        request.source_format,
        deadline_monotonic=request.deadline_monotonic,
        cancellation=request.cancellation,
    )
    observed = None
    try:
        candidate = source.convert(
            classifications=request.import_classifications,
            public_values=request.import_public_values,
        )
        observed = observer.observe(
            candidate,
            deadline_monotonic=request.deadline_monotonic,
            cancellation=request.cancellation,
            scrub_set=scrub_set,
        )
        # External whole-bundle equivalence does not inherit native partial
        # component diagnostics as permission to silently drop active components.
        if observed.diagnostics:
            raise PluginImportError("转换后的组件未通过原生校验，不能安装为完整插件。")
        observed.source_import = source
        return observed
    except BaseException:
        if observed is not None:
            observed.close()
        source.close()
        raise


class PluginImportError(ValueError):
    """Only field paths/product explanations, never source literal values."""


class PluginSourceImport:
    def __init__(self, source, source_format, *, deadline_monotonic, cancellation):
        if source_format not in SOURCE_MANIFESTS or source_format == "native":
            raise PluginImportError("请选择一种外部插件发行格式。")
        self.source = source
        self.format = source_format
        self.deadline = deadline_monotonic
        self.cancellation = cancellation
        self.descriptor = _open_source_root(source)
        self.temporary = None
        try:
            info = os.fstat(self.descriptor)
            self.root = (info.st_dev, info.st_ino)
            self.entries = _observe_membership(
                self.descriptor,
                deadline_monotonic=self.deadline,
                cancellation=self.cancellation,
            )
            self.by_path = {entry.relative_path: entry for entry in self.entries}
            self.manifest = self.read_json(SOURCE_MANIFESTS[source_format])
            if not isinstance(self.manifest, dict):
                raise PluginImportError("所选插件 manifest 必须是对象。")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None

    def revalidate(self):
        _revalidate_source(
            self.source,
            self.descriptor,
            root=self.root,
            expected_entries=self.entries,
            deadline_monotonic=self.deadline,
            cancellation=self.cancellation,
            enforce_managed_admission=True,
        )

    def path(self, value):
        if not isinstance(value, str) or not value or "\\" in value:
            raise PluginImportError("组件路径必须是包内相对路径。")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise PluginImportError("组件路径不能逃出所选插件根。")
        return path

    def read_json(self, value):
        entry = self.by_path.get(self.path(value))
        if entry is None or entry.kind is not PackageEntryKind.REGULAR_FILE:
            raise PluginImportError(f"找不到声明的组件文件：{value}")
        raw = _read_bounded_entry(
            self.descriptor,
            entry,
            maximum=MAXIMUM_PLUGIN_JSON_BYTES,
            deadline_monotonic=self.deadline,
            cancellation=self.cancellation,
        )
        return _jsonc(raw.decode("utf-8"))

    def selected(self, name, default):
        # Absence uses only this format's default. Empty object/array is explicit.
        value = self.manifest[name] if name in self.manifest else default
        return self.read_json(value) if isinstance(value, str) else value

    def component_plan(self):
        manifest = self.manifest
        display = {
            "$schema",
            "name",
            "version",
            "description",
            "author",
            "homepage",
            "repository",
            "license",
            "keywords",
            "displayName",
            "logo",
            "category",
            "tags",
            "interface",
        }
        active = {
            "skills",
            "mcpServers",
            "hooks",
            "variables",
            "commands",
            "agents",
            "rules",
            "apps",
            "channels",
            "outputStyles",
            "lspServers",
            "minClientVersions",
        }
        unknown = set(manifest) - display - active
        if unknown:
            raise PluginImportError(
                "未映射的 manifest 字段：" + ", ".join(sorted(unknown))
            )
        for name in active - {
            "skills",
            "mcpServers",
            "hooks",
            "variables",
            "minClientVersions",
        }:
            default_path = PurePosixPath(name)
            if (
                manifest.get(name)
                or name not in manifest
                and default_path in self.by_path
            ):
                raise PluginImportError(
                    f"{name} 依赖宿主行为，不能宣称整包等价；可另行导入独立 MCP 或技能。"
                )
        # Version requirements describe the source host; they are displayed, not
        # reinterpreted as a Pulsara runtime version or an executable dependency.
        default_mcp = ".mcp.json" if PurePosixPath(".mcp.json") in self.by_path else {}
        mcp = self.selected("mcpServers", default_mcp)
        if mcp == []:
            mcp = {}
        if not isinstance(mcp, dict):
            raise PluginImportError("mcpServers 需要文件路径或对象。")
        drafts = read_mcp_import(
            json.dumps(mcp), shape="mcpServers" if "mcpServers" in mcp else "map"
        )
        for draft in drafts:
            if draft.issues:
                raise PluginImportError(
                    "; ".join(
                        f"{draft.server_id}.{item.path}: {item.message}"
                        for item in draft.issues
                    )
                )
        default_hooks = (
            "hooks/hooks.json"
            if PurePosixPath("hooks/hooks.json") in self.by_path
            else {}
        )
        hooks = self.selected("hooks", default_hooks)
        if hooks == [] or hooks == {}:
            hooks = {"hooks": {}}
        if not isinstance(hooks, dict) or set(hooks) != {"hooks"}:
            raise PluginImportError(
                "Hooks 需要可等价的 hooks 对象；不能忽略未知执行字段。"
            )
        skills_value = manifest.get(
            "skills", "skills" if PurePosixPath("skills") in self.by_path else []
        )
        paths = [skills_value] if isinstance(skills_value, str) else skills_value
        if not isinstance(paths, list) or any(
            not isinstance(item, str) for item in paths
        ):
            raise PluginImportError("skills 需要目录路径或路径数组。")
        skills = []
        for value in paths:
            root = self.path(value)
            if root / "SKILL.md" in self.by_path:
                skills.append(root)
            else:
                found = [
                    path.parent
                    for path in self.by_path
                    if path.name == "SKILL.md" and path.parent.parent == root
                ]
                if not found:
                    raise PluginImportError(f"技能目录没有 SKILL.md：{value}")
                skills.extend(sorted(found))
        if len({path.name for path in skills}) != len(skills):
            raise PluginImportError("所选技能名称重复，请选择一个发行目录。")
        return drafts, hooks, tuple(skills)

    def preview(self):
        if (
            not isinstance(self.manifest.get("name"), str)
            or not self.manifest["name"].strip()
        ):
            raise PluginImportError("manifest 的 name 必须是非空文字。")
        drafts, hooks, skills = self.component_plan()
        mcp = []
        for draft in drafts:
            preview = draft.preview()
            for field in preview["fields"]:
                if field["target"] == "endpoint" and not dict(draft.inputs)["endpoint"]:
                    field["variables"] = [
                        {
                            "name": draft.server_id + "_ENDPOINT",
                            "has_default": False,
                            "environment": False,
                            "file_path": None,
                        }
                    ]
            mcp.append(preview)
        return {
            "name": self.manifest.get("name"),
            "source_format": self.format,
            "skills": [path.name for path in skills],
            "hooks": list(hooks["hooks"]),
            "mcp": mcp,
            "notices": ["仅转换所选发行版；资源和脚本不执行，安装后保持关闭。"],
        }

    def convert(self, *, classifications=(), public_values=()):
        drafts, hooks, skills = self.component_plan()
        categories, values = dict(classifications), dict(public_values)
        if len(categories) != len(classifications) or len(values) != len(public_values):
            raise PluginImportError("导入参数重复。")
        variables = self.manifest.get("variables", {})
        if variables and (
            not isinstance(variables, dict)
            or set(variables) - {"type", "properties", "required"}
            or variables.get("type") != "object"
        ):
            raise PluginImportError("variables 只支持具名文字输入。")
        definitions = variables.get("properties", {})
        required = variables.get("required", [])
        if not isinstance(definitions, dict) or not isinstance(required, list):
            raise PluginImportError("variables 定义无效。")
        for definition in definitions.values():
            if (
                not isinstance(definition, dict)
                or set(definition) - {"type", "title", "description", "default"}
                or definition.get("type") != "string"
            ):
                raise PluginImportError(
                    "variables 只支持文字、标题、说明和普通默认值。"
                )
        servers, extensions = {}, {}
        used_categories, used_values = set(), set()
        for draft in drafts:
            inputs, targets = {}, []
            cfg = draft.config
            transport = cfg["transport"]
            kind = transport["type"]
            if cfg.get("enabled") is not True:
                raise PluginImportError(
                    f"{draft.server_id}.enabled 无法由随插件启用的组件等价表达。"
                )
            if kind == "stdio":
                server = {
                    "type": "stdio",
                    "command": self.root_template(transport["command"]),
                    "args": [self.root_template(arg) for arg in transport["args"]],
                    "env": {},
                }
                cwd = self.root_template(transport["cwd"])
                server["cwd"] = (
                    "${PLUGIN_ROOT}"
                    if cwd == "."
                    else cwd
                    if cwd.startswith("${PLUGIN_ROOT}")
                    else "${PLUGIN_ROOT}/" + str(self.path(cwd))
                )
            else:
                server = {
                    "type": "sse" if kind == "sse" else "streamable-http",
                    "url": "",
                    "headers": {},
                }
            for target, raw in draft.inputs:
                key = draft.server_id + "." + target
                category = (
                    "private"
                    if target in draft.private_targets
                    else "public"
                    if target == "endpoint" or target.startswith("oauth:")
                    else categories.get(key)
                )
                if key in categories:
                    used_categories.add(key)
                    if category == "private" and categories[key] != "private":
                        raise PluginImportError(f"{key} 必须作为私有输入。")
                if category not in {"public", "private"}:
                    raise PluginImportError(f"请先确认 {key} 是普通值还是私有值。")
                parts, rendered = [], []
                template = parse_import_template(raw)
                if not template and target == "endpoint":
                    template = (ImportVariable(draft.server_id + "_ENDPOINT"),)
                if category == "private" and not any(
                    isinstance(part, ImportVariable) for part in template
                ):
                    raise PluginImportError(
                        f"{key} 包含凭据字面量；请使用参数化来源，安装后再填写凭据。"
                    )
                for part in template:
                    if isinstance(part, str):
                        parts.append({"literal": part})
                        rendered.append(part)
                        continue
                    if part.file_path:
                        raise PluginImportError(
                            f"{key} 不会读取本机文件；可单独导入该 MCP 并选择文件。"
                        )
                    definition = definitions.get(part.name, {})
                    default = values.get(
                        part.name,
                        part.default
                        if part.default is not None
                        else definition.get("default"),
                    )
                    if part.name in values:
                        used_values.add(part.name)
                    if category == "private" and default is not None:
                        raise PluginImportError(
                            f"{part.name} 是私有输入，不接受安装参数或包内默认值。"
                        )
                    if category == "public" and not isinstance(default, str):
                        raise PluginImportError(f"请填写普通输入：{part.name}")
                    item = {
                        "name": part.name,
                        "title": definition.get("title", part.name),
                        "private": category == "private",
                        "required": part.name in required or part.default is None,
                        "default": default,
                    }
                    if part.name in inputs and inputs[part.name] != item:
                        raise PluginImportError(
                            f"{part.name} 的公开/私有用途或默认值冲突。"
                        )
                    inputs[part.name] = item
                    parts.append({"input": part.name})
                    if category == "public":
                        rendered.append(default)
                target_kind, target_name = (
                    ("endpoint", "endpoint")
                    if target == "endpoint"
                    else target.split(":", 1)
                )
                has_input = any("input" in part for part in parts)
                if has_input or target_kind == "oauth":
                    targets.append(
                        {"kind": target_kind, "name": target_name, "parts": parts}
                    )
                if category == "public":
                    value = "".join(rendered)
                    if target_kind == "endpoint":
                        server["url"] = value
                    elif target_kind == "header":
                        server["headers"][target_name] = value
                    elif target_kind == "env":
                        server["env"][target_name] = value
            if cfg["auth"]["type"] == "oauth" and not any(
                target["kind"] == "oauth" for target in targets
            ):
                targets.append(
                    {
                        "kind": "oauth",
                        "name": "redirect_uri",
                        "parts": [{"literal": "http://127.0.0.1:17839/callback"}],
                    }
                )
            elif cfg["auth"]["type"] == "bearer":
                name = cfg["auth"]["reference"]["name"]
                inputs[name] = {
                    "name": name,
                    "title": name,
                    "private": True,
                    "required": True,
                    "default": None,
                }
                targets.append(
                    {
                        "kind": "header",
                        "name": "Authorization",
                        "parts": [{"literal": "Bearer "}, {"input": name}],
                    }
                )
            servers[draft.server_id] = server
            if targets:
                extensions[draft.server_id] = {
                    "inputs": list(inputs.values()),
                    "targets": targets,
                }
        if set(categories) != used_categories or set(values) != used_values:
            raise PluginImportError("导入包含不属于所选组件的分类或输入。")
        self.temporary = TemporaryDirectory(prefix="pulsara-plugin-import-")
        destination = Path(self.temporary.name)
        # Preserve relative resources in place, but only selected component roots
        # become native active surfaces. Manifests/configs are generated once.
        excluded = {PurePosixPath(value) for value in SOURCE_MANIFESTS.values()} | {
            PurePosixPath("mcp.json"),
            PurePosixPath(".mcp.json"),
        }
        for entry in self.entries:
            path = entry.relative_path
            if (
                entry.kind is not PackageEntryKind.REGULAR_FILE
                or path in excluded
                or path.parts[0] in {"skills", "dev.pulsara"}
            ):
                continue
            self.copy_file(entry, destination / path)
        for skill in skills:
            for entry in self.entries:
                if (
                    entry.kind is PackageEntryKind.REGULAR_FILE
                    and entry.relative_path.is_relative_to(skill)
                ):
                    self.copy_file(
                        entry,
                        destination
                        / "skills"
                        / skill.name
                        / entry.relative_path.relative_to(skill),
                    )
        native = {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        }
        for field in (
            "name",
            "version",
            "description",
            "author",
            "homepage",
            "repository",
            "license",
            "keywords",
        ):
            if field in self.manifest:
                native[field] = self.manifest[field]
        self.write_json(destination / "plugin.json", native)
        self.write_json(
            destination / "mcp.json",
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": servers,
            },
        )
        if extensions:
            self.write_json(
                destination / "dev.pulsara/mcp/connection-inputs.json",
                {"servers": extensions},
            )
        if hooks["hooks"]:
            self.write_json(destination / "dev.pulsara/hooks/hooks.json", hooks)
        self.revalidate()
        return destination

    def root_template(self, value):
        if not isinstance(value, str):
            raise PluginImportError("命令和参数需要文字。")
        token = {
            "claude": "CLAUDE_PLUGIN_ROOT",
            "cursor": "CURSOR_PLUGIN_ROOT",
            "codex": "PLUGIN_ROOT",
        }[self.format]
        value = value.replace("${" + token + "}", "${PLUGIN_ROOT}")
        if any(
            isinstance(part, ImportVariable) and part.name != "PLUGIN_ROOT"
            for part in parse_import_template(value)
        ):
            raise PluginImportError("连接输入不能决定命令、参数或工作目录。")
        return value

    def copy_file(self, entry, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = _open_regular_entry(self.descriptor, entry)
        try:
            with destination.open("xb") as output:
                while True:
                    _check_abort(self.deadline, self.cancellation)
                    chunk = os.read(descriptor, COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    output.write(chunk)
            if not _entry_matches(entry, os.fstat(descriptor)):
                raise PluginPackageRaced
            destination.chmod(0o700 if entry.executable else 0o600)
        finally:
            os.close(descriptor)

    @staticmethod
    def write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
