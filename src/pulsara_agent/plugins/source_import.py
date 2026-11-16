"""One-shot external manifest conversion. Runtime only sees native packages.

The selected source remains held and revalidated through publication. No import
registry, executable template language, service-specific adapter or source edit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from pulsara_agent.hooks.config_parser import (
    HookConfigParseError,
    parse_hook_declarations,
)
from .contracts import PluginDiagnostic, PluginDiagnosticCode
from .hook_import import merge_hook_sources
from .connection_inputs import parse_connection_inputs
from pulsara_agent.primitives.bounded_json import JsonBoundExceeded
from pulsara_agent.capability.local_skills import (
    MAX_SKILL_FILE_BYTES,
    parse_skill_document,
)
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
    _check_abort,
    MAXIMUM_PLUGIN_JSON_BYTES,
    PackageEntryKind,
    PluginSourceObserver,
    PluginPackageInvalid,
    prepare_local_source_path,
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
    source = prepare_local_source_path(source).resolve()
    descriptor = _open_source_root(source)
    try:
        entries = _observe_membership(
            descriptor,
            source_path=source,
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
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
                        "notices": [item.message for item in observed.diagnostics],
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
            secret_scan_source_path=request.source_path,
        )
        # The native observer already isolates component failures. Adoption must
        # not promote these diagnostics back into an unrelated package failure.
        observed.diagnostics = (
            *observed.diagnostics,
            *source.diagnostics,
            *source.unsupported_components(),
        )
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
        self.source = prepare_local_source_path(source).resolve()
        self.format = source_format
        self.deadline = deadline_monotonic
        self.cancellation = cancellation
        self.descriptor = _open_source_root(self.source)
        self.temporary = None
        self.diagnostics = []
        self._plan = None
        self._skill_names = {}
        try:
            info = os.fstat(self.descriptor)
            self.root = (info.st_dev, info.st_ino)
            self.entries = _observe_membership(
                self.descriptor,
                source_path=self.source,
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
        try:
            raw = _read_bounded_entry(
                self.descriptor,
                entry,
                maximum=MAXIMUM_PLUGIN_JSON_BYTES,
                deadline_monotonic=self.deadline,
                cancellation=self.cancellation,
            )
        except JsonBoundExceeded as exc:
            raise PluginImportError(f"组件文件 {value} 超过配置读取边界。") from exc
        try:
            return _jsonc(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise PluginImportError(
                f"组件文件 {value} 不是有效的 JSON/JSONC。"
            ) from exc

    def selected(self, name, default):
        # Absence uses only this format's default. Empty object/array is explicit.
        value = self.manifest[name] if name in self.manifest else default
        return self.read_json(value) if isinstance(value, str) else value

    def report(self, code, message, *, path=None, component=None):
        diagnostic = PluginDiagnostic(
            code, message, path or SOURCE_MANIFESTS[self.format], component
        )
        if diagnostic not in self.diagnostics:
            self.diagnostics.append(diagnostic)

    def component_plan(self):
        if self._plan is not None:
            return self._plan
        manifest = self.manifest
        default_mcp = ".mcp.json" if PurePosixPath(".mcp.json") in self.by_path else {}
        drafts = ()
        try:
            mcp = self.selected("mcpServers", default_mcp)
            if mcp == []:
                mcp = {}
            if not isinstance(mcp, dict):
                raise PluginImportError("mcpServers 需要文件路径或对象。")
            # A foreign host's presentation note does not change its connection.
            # Keep native Agent Plugins and standalone MCP validation unchanged.
            server_map = mcp.get("mcpServers") if "mcpServers" in mcp else mcp
            if isinstance(server_map, dict):
                normalized = {}
                for server_id, config in server_map.items():
                    if isinstance(config, dict) and "note" in config:
                        self.report(
                            PluginDiagnosticCode.MANIFEST_UNKNOWN_FIELD_IGNORED,
                            f"MCP {server_id} 的说明字段 note 已忽略。",
                            component=server_id,
                        )
                        config = {
                            key: value for key, value in config.items() if key != "note"
                        }
                    normalized[server_id] = config
                mcp = (
                    {**mcp, "mcpServers": normalized}
                    if "mcpServers" in mcp
                    else normalized
                )
            candidates = read_mcp_import(
                json.dumps(mcp), shape="mcpServers" if "mcpServers" in mcp else "map"
            )
            valid = []
            for draft in candidates:
                if draft.issues:
                    self.report(
                        PluginDiagnosticCode.MCP_SERVER_INVALID,
                        f"MCP {draft.server_id} 未导入："
                        + "; ".join(
                            f"{item.path}: {item.message}" for item in draft.issues
                        ),
                        component=draft.server_id,
                    )
                else:
                    valid.append(draft)
            drafts = tuple(valid)
        except (ValueError, UnicodeError, FileNotFoundError, PermissionError) as exc:
            self.report(
                PluginDiagnosticCode.MCP_COMPONENT_INVALID,
                f"MCP 配置未导入：{exc}",
                component="mcpServers",
            )

        default_hooks = (
            "hooks/hooks.json"
            if PurePosixPath("hooks/hooks.json") in self.by_path
            else {}
        )
        hooks = merge_hook_sources(
            manifest.get("hooks", default_hooks),
            read_json=self.read_json,
            report=lambda path, message: self.report(
                PluginDiagnosticCode.EXTENSIONS_FIELD_IGNORED,
                message,
                path=path,
                component="hooks",
            ),
        )
        skills_value = manifest.get(
            "skills", "skills" if PurePosixPath("skills") in self.by_path else []
        )
        paths = [skills_value] if isinstance(skills_value, str) else skills_value
        if not isinstance(paths, list):
            self.report(
                PluginDiagnosticCode.COMPONENT_KIND_INVALID,
                "Skill 配置未导入：skills 需要目录路径或路径数组。",
                component="skills",
            )
            paths = []
        skills, names = [], set()
        for value in paths:
            _check_abort(self.deadline, self.cancellation)
            try:
                root = self.path(value)
                found = (
                    [root]
                    if root / "SKILL.md" in self.by_path
                    else sorted(
                        path.parent
                        for path in self.by_path
                        if path.name == "SKILL.md" and path.parent.parent == root
                    )
                )
                if not found:
                    raise PluginImportError(f"技能目录没有 SKILL.md：{value}")
                for skill in found:
                    if (
                        self.by_path[skill / "SKILL.md"].kind
                        is not PackageEntryKind.REGULAR_FILE
                    ):
                        self.report(
                            PluginDiagnosticCode.COMPONENT_KIND_INVALID,
                            f"Skill {skill} 未导入：SKILL.md 不是可读的包内普通文件。",
                            path=str(skill),
                            component="skills",
                        )
                        continue
                    if skill in skills:
                        continue
                    document = self.by_path[skill / "SKILL.md"]
                    try:
                        raw = _read_bounded_entry(
                            self.descriptor,
                            document,
                            maximum=MAX_SKILL_FILE_BYTES,
                            deadline_monotonic=self.deadline,
                            cancellation=self.cancellation,
                        )
                        parsed = parse_skill_document(raw)
                    except JsonBoundExceeded:
                        parsed = parse_skill_document(b"x" * (MAX_SKILL_FILE_BYTES + 1))
                    if parsed.parsed is None:
                        self.report(
                            PluginDiagnosticCode.COMPONENT_KIND_INVALID,
                            f"Skill {skill}/SKILL.md 未导入："
                            + "; ".join(item.message for item in parsed.diagnostics),
                            path=str(skill / "SKILL.md"),
                            component="skills",
                        )
                        continue
                    name = parsed.parsed.name
                    if name in names:
                        self.report(
                            PluginDiagnosticCode.COMPONENT_KIND_INVALID,
                            f"Skill {skill}/SKILL.md 未导入：声明名称 {name} 与已选 Skill 冲突。",
                            path=str(skill / "SKILL.md"),
                            component="skills",
                        )
                        continue
                    names.add(name)
                    self._skill_names[skill] = name
                    skills.append(skill)
            except (
                ValueError,
                UnicodeError,
                FileNotFoundError,
                PermissionError,
            ) as exc:
                self.report(
                    PluginDiagnosticCode.COMPONENT_KIND_INVALID,
                    f"Skill 未导入：{exc}",
                    component="skills",
                )
        self._copy_entries, selected_skills = self.copy_plan(tuple(skills))
        self._plan = drafts, hooks, selected_skills
        return self._plan

    def unsupported_components(self):
        """Only selected-format declarations/conventions, never sibling hosts."""
        known = {
            "commands",
            "agents",
            "rules",
            "apps",
            "channels",
            "outputStyles",
            "lspServers",
        }
        metadata = {
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
        supported = {"skills", "mcpServers", "hooks", "variables", "minClientVersions"}
        names = {name for name in known if self.manifest.get(name)}
        # Claude's implicit host components are Markdown; Ponytail's Gemini
        # commands/*.toml is an inert resource in a Claude/Codex distribution.
        if self.format == "claude":
            for name in {"commands", "agents", "outputStyles"}:
                if name not in self.manifest and any(
                    path.suffix == ".md" and path.is_relative_to(PurePosixPath(name))
                    for path in self.by_path
                ):
                    names.add(name)
        unknown = set(self.manifest) - metadata - supported - known
        return tuple(
            PluginDiagnostic(
                PluginDiagnosticCode.MANIFEST_UNKNOWN_FIELD_IGNORED
                if name in unknown
                else PluginDiagnosticCode.EXTENSIONS_FIELD_IGNORED,
                f"未支持宿主功能 {name}：不会注册或执行；仅导入支持的组件。",
                SOURCE_MANIFESTS[self.format],
                name,
            )
            for name in sorted(names | unknown)
        )

    def parse_import_hooks(self, hooks):
        return parse_hook_declarations(json.dumps(hooks).encode())

    def preview(self):
        if (
            not isinstance(self.manifest.get("name"), str)
            or not self.manifest["name"].strip()
        ):
            raise PluginImportError("manifest 的 name 必须是非空文字。")
        drafts, hooks, skills = self.component_plan()
        mcp = []
        try:
            parsed_hooks = self.parse_import_hooks(hooks)
        except HookConfigParseError as exc:
            self.report(
                PluginDiagnosticCode.COMPONENT_KIND_INVALID,
                f"Hook 配置未导入：{exc}",
                component="hooks",
            )
            parsed_hooks = None
        self.native_metadata()
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
            "skills": [self._skill_names[path] for path in skills],
            "hooks": list(
                dict.fromkeys(
                    item.event_type.external_name
                    for item in (parsed_hooks.definitions if parsed_hooks else ())
                )
            ),
            "mcp": mcp,
            "notices": [
                "仅导入所选发行版支持的组件；资源和脚本不执行，安装后保持关闭。"
            ]
            + [item.message for item in self.diagnostics]
            + [item.message for item in self.unsupported_components()]
            + [
                item.message
                for item in (parsed_hooks.diagnostics if parsed_hooks else ())
            ]
            + (
                [
                    "Hook 提供插件目录兼容变量；工具名与参数仍为 Pulsara 原生契约，脚本行为尚未验证。"
                ]
                if hooks["hooks"]
                else []
            ),
        }

    def convert(self, *, classifications=(), public_values=()):
        drafts, hooks, skills = self.component_plan()
        categories, values = dict(classifications), dict(public_values)
        if len(categories) != len(classifications) or len(values) != len(public_values):
            raise PluginImportError("导入参数重复。")
        definitions, required, invalid_definitions = self.input_definitions()
        known_categories = {
            f"{draft.server_id}.{target}"
            for draft in drafts
            for target, _ in draft.inputs
        }
        known_values = set(definitions) | invalid_definitions
        for draft in drafts:
            for target, raw in draft.inputs:
                if target == "endpoint" and not raw:
                    known_values.add(draft.server_id + "_ENDPOINT")
                try:
                    known_values.update(
                        part.name
                        for part in parse_import_template(raw)
                        if isinstance(part, ImportVariable)
                    )
                except ValueError:
                    pass
        if set(categories) - known_categories or set(values) - known_values:
            raise PluginImportError("导入包含不属于所选组件的分类或输入。")
        servers, extensions = {}, {}
        for draft in drafts:
            _check_abort(self.deadline, self.cancellation)
            try:
                server, connection = self.convert_server(
                    draft,
                    categories=categories,
                    values=values,
                    definitions=definitions,
                    required=required,
                    invalid_definitions=invalid_definitions,
                )
            except (ValueError, TypeError, KeyError) as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, PluginImportError)
                    else "连接配置结构无效。"
                )
                self.report(
                    PluginDiagnosticCode.MCP_SERVER_INVALID,
                    f"MCP {draft.server_id} 未导入：{reason}",
                    component=draft.server_id,
                )
                continue
            servers[draft.server_id] = server
            if connection is not None:
                extensions[draft.server_id] = connection
        self.temporary = TemporaryDirectory(prefix="pulsara-plugin-import-")
        destination = Path(self.temporary.name)
        # Preserve source resource layout and contained links through the same
        # copier as native publication. Selected custom Skills get a discovery
        # alias, rather than moving scripts and changing their realpath parents.
        from .package_store import _copy_package_entries

        destination_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
        try:
            _copy_package_entries(
                self.descriptor,
                self._copy_entries,
                destination_fd,
                deadline_monotonic=self.deadline,
                cancellation=self.cancellation,
            )
        finally:
            os.close(destination_fd)
        for skill in skills:
            _check_abort(self.deadline, self.cancellation)
            canonical = PurePosixPath("skills") / self._skill_names[skill]
            if skill != canonical:
                alias = destination / canonical
                alias.parent.mkdir(parents=True, exist_ok=True)
                alias.symlink_to(
                    os.path.relpath(skill.as_posix(), canonical.parent.as_posix()),
                    target_is_directory=True,
                )
        native = {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        }
        native.update(self.native_metadata())
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
        # This sole generated source also represents an explicit empty choice.
        # Vendor manifests remain inert resources and cannot trigger fallback.
        self.write_json(destination / "dev.pulsara/hooks/hooks.json", hooks)
        self.revalidate()
        return destination

    def copy_plan(self, skills):
        selected = list(skills)
        omitted_links = set()
        generated = {PurePosixPath("plugin.json"), PurePosixPath("mcp.json")}
        while True:
            _check_abort(self.deadline, self.cancellation)
            entries = tuple(
                entry
                for entry in self.entries
                if entry.relative_path not in generated
                and entry.relative_path.parts[0] != "dev.pulsara"
                and not any(
                    entry.relative_path.is_relative_to(path) for path in omitted_links
                )
                and (
                    entry.relative_path.parts[0] != "skills"
                    or any(
                        entry.relative_path.is_relative_to(skill)
                        or skill.is_relative_to(entry.relative_path)
                        for skill in selected
                    )
                )
            )
            copied = {
                entry.relative_path
                for entry in entries
                if entry.kind
                in {PackageEntryKind.DIRECTORY, PackageEntryKind.REGULAR_FILE}
            }
            available = (
                copied
                | generated
                | {
                    PurePosixPath("dev.pulsara"),
                    PurePosixPath("dev.pulsara/hooks"),
                    PurePosixPath("dev.pulsara/hooks/hooks.json"),
                }
            )
            broken = {
                entry.relative_path
                for entry in entries
                if entry.link_target is not None
                and entry.resolved_relative_path not in available
            }
            for entry in entries:
                if (
                    entry.link_target is not None
                    and entry.resolved_relative_path in available - copied
                ):
                    self.report(
                        PluginDiagnosticCode.EXTENSIONS_FIELD_IGNORED,
                        f"资源链接 {entry.relative_path} 引用生成配置 {entry.resolved_relative_path}；安装后的配置按所选发行版规范化。",
                        path=str(entry.relative_path),
                    )
            rejected = []
            for skill in selected:
                _check_abort(self.deadline, self.cancellation)
                canonical = PurePosixPath("skills") / self._skill_names[skill]
                conflict = skill != canonical and canonical in copied
                missing = (
                    skill not in copied
                    or not skill.name
                    or skill == PurePosixPath("skills")
                )
                bad_link = next(
                    (
                        path
                        for path in sorted(broken)
                        if path.is_relative_to(skill) or skill.is_relative_to(path)
                    ),
                    None,
                )
                if conflict or missing or bad_link is not None:
                    reason = (
                        f"链接 {bad_link} 指向未导入的资源"
                        if bad_link is not None
                        else "无法在固定 skills 目录保留原文件布局"
                    )
                    self.report(
                        PluginDiagnosticCode.COMPONENT_KIND_INVALID,
                        f"Skill {skill} 未导入：{reason}。",
                        path=str(skill),
                        component="skills",
                    )
                    rejected.append(skill)
            if rejected:
                selected = [skill for skill in selected if skill not in rejected]
                continue
            if broken:
                for path in sorted(broken):
                    self.report(
                        PluginDiagnosticCode.COMPONENT_KIND_INVALID,
                        f"资源链接 {path} 未导入：目标不在本次保留的文件布局中。",
                        path=str(path),
                    )
                omitted_links.update(broken)
                continue
            return entries, tuple(selected)

    def native_metadata(self):
        native = {"name": self.manifest.get("name")}
        for field in ("version", "description", "homepage", "repository", "license"):
            value = self.manifest.get(field)
            if field not in self.manifest:
                continue
            if isinstance(value, str):
                native[field] = value
            else:
                self.report(
                    PluginDiagnosticCode.MANIFEST_UNKNOWN_FIELD_IGNORED,
                    f"可选元数据 {field} 不是文字，已忽略。",
                    component=field,
                )
        author = self.manifest.get("author")
        if isinstance(author, str):
            native["author"] = {"name": author}
        elif isinstance(author, dict):
            native["author"] = {
                key: author[key]
                for key in ("name", "email", "url")
                if isinstance(author.get(key), str)
            }
            if set(author) != set(native["author"]):
                self.report(
                    PluginDiagnosticCode.MANIFEST_UNKNOWN_FIELD_IGNORED,
                    "author 中未识别或无效的可选字段已忽略。",
                    component="author",
                )
        elif "author" in self.manifest:
            self.report(
                PluginDiagnosticCode.MANIFEST_UNKNOWN_FIELD_IGNORED,
                "author 元数据不受支持，已忽略。",
                component="author",
            )
        keywords = self.manifest.get("keywords")
        if isinstance(keywords, list) and all(
            isinstance(item, str) for item in keywords
        ):
            native["keywords"] = keywords
        elif "keywords" in self.manifest:
            self.report(
                PluginDiagnosticCode.MANIFEST_UNKNOWN_FIELD_IGNORED,
                "keywords 不是文字数组，已忽略。",
                component="keywords",
            )
        return native

    def input_definitions(self):
        variables = self.manifest.get("variables", {})
        if not isinstance(variables, dict) or (
            variables
            and (
                set(variables) - {"type", "properties", "required"}
                or variables.get("type") != "object"
            )
        ):
            self.report(
                PluginDiagnosticCode.EXTENSIONS_FIELD_IGNORED,
                "variables 配置不受支持；依赖它且缺少输入的 MCP 服务未导入。",
                component="variables",
            )
            return {}, [], set()
        raw = variables.get("properties", {})
        required = variables.get("required", [])
        if (
            not isinstance(raw, dict)
            or not isinstance(required, list)
            or any(not isinstance(item, str) for item in required)
        ):
            self.report(
                PluginDiagnosticCode.EXTENSIONS_FIELD_IGNORED,
                "variables 定义无效；依赖它且缺少输入的 MCP 服务未导入。",
                component="variables",
            )
            return {}, [], set()
        definitions, invalid = {}, set()
        for name, definition in raw.items():
            if (
                not isinstance(definition, dict)
                or set(definition) - {"type", "title", "description", "default"}
                or definition.get("type") != "string"
                or any(
                    key in definition and not isinstance(definition[key], str)
                    for key in ("title", "description", "default")
                )
            ):
                invalid.add(name)
                self.report(
                    PluginDiagnosticCode.EXTENSIONS_FIELD_IGNORED,
                    f"连接输入 {name} 的定义不受支持，依赖它的 MCP 服务未导入。",
                    component="variables",
                )
            else:
                definitions[name] = definition
        return definitions, required, invalid

    def convert_server(
        self, draft, *, categories, values, definitions, required, invalid_definitions
    ):
        inputs, targets = {}, []
        cfg = draft.config
        transport = cfg["transport"]
        kind = transport["type"]
        if cfg.get("enabled") is not True:
            raise PluginImportError(
                f"{draft.server_id}.enabled 无法由随插件启用的组件等价表达。"
            )
        if kind == "stdio":
            args = transport["args"]
            if not isinstance(args, list) or any(
                not isinstance(arg, str) for arg in args
            ):
                raise PluginImportError("args 需要文字数组。")
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
                if part.name in invalid_definitions:
                    raise PluginImportError(f"连接输入 {part.name} 的定义不受支持。")
                definition = definitions.get(part.name, {})
                default = values.get(
                    part.name,
                    part.default
                    if part.default is not None
                    else definition.get("default"),
                )
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
        connection = (
            {"inputs": list(inputs.values()), "targets": targets} if targets else None
        )
        if connection is not None:
            # Reuse the native input parser per server, so one malformed target
            # cannot invalidate the combined MCP component on later adoption.
            parse_connection_inputs(
                {"servers": {draft.server_id: connection}},
                server_ids=(draft.server_id,),
            )
        return server, connection

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

    @staticmethod
    def write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
