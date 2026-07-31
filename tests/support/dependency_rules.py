"""Canonical AST dependency observations used by the D4 architecture gate."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True, slots=True, order=True)
class ImportObservation:
    source_module: str
    target_module: str
    import_kind: str
    enclosing_qualname: str
    normalized_import_ast_fingerprint: str
    equal_ast_occurrence_ordinal: int
    source_path: str
    line: int
    column: int
    observation_id: str
    observation_fingerprint: str

    @property
    def source_package(self) -> str:
        return _package(self.source_module)

    @property
    def target_package(self) -> str:
        return _package(self.target_module)


def scan_pulsara_imports(repo_root: Path) -> tuple[ImportObservation, ...]:
    source_root = repo_root / "src"
    observations: list[ImportObservation] = []
    for path in sorted((source_root / "pulsara_agent").rglob("*.py")):
        source_module = _module_for_path(path, source_root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _CanonicalImportScanner(
            source_module=source_module,
            source_is_package=path.name == "__init__.py",
            source_path=path.relative_to(repo_root).as_posix(),
            observations=observations,
        ).visit(tree)
    return tuple(sorted(observations))


class _CanonicalImportScanner(ast.NodeVisitor):
    def __init__(
        self,
        *,
        source_module: str,
        source_is_package: bool,
        source_path: str,
        observations: list[ImportObservation],
    ) -> None:
        self._source_module = source_module
        self._source_is_package = source_is_package
        self._source_path = source_path
        self._observations = observations
        self._owner_path: list[str] = []
        self._occurrences: dict[tuple[str, str, str, str], int] = {}

    @property
    def _enclosing_qualname(self) -> str:
        return ".".join(self._owner_path) if self._owner_path else "<module>"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._visit_owner(node.name, node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_owner(node.name, node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_owner(node.name, node)

    def _visit_owner(
        self,
        name: str,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self._owner_path.append(name)
        self.generic_visit(node)
        self._owner_path.pop()

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self._record(
                node=node,
                target=alias.name,
                import_kind="import",
                normalized_payload={
                    "names": ((alias.name, alias.asname),),
                },
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        target = _resolve_import_from(
            self._source_module,
            node,
            source_is_package=self._source_is_package,
        )
        if target:
            self._record(
                node=node,
                target=target,
                import_kind="from",
                normalized_payload={
                    "level": node.level,
                    "names": tuple((alias.name, alias.asname) for alias in node.names),
                },
            )

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        call_kind = _dynamic_import_call_kind(node.func)
        target = _literal_dynamic_import_target(
            node,
            source_module=self._source_module,
            source_is_package=self._source_is_package,
        )
        if call_kind is not None and target is not None:
            import_kind = (
                "package_getattr"
                if self._owner_path and self._owner_path[-1] == "__getattr__"
                else call_kind
            )
            self._record(
                node=node,
                target=target,
                import_kind=import_kind,
                normalized_payload={
                    "call": ast.dump(
                        node, annotate_fields=True, include_attributes=False
                    ),
                },
            )
        self.generic_visit(node)

    def _record(
        self,
        *,
        node: ast.AST,
        target: str,
        import_kind: str,
        normalized_payload: object,
    ) -> None:
        if not _is_scanned_target(target):
            return
        normalized_fingerprint = _fingerprint(
            "d4-normalized-import-ast:v1",
            {
                "import_kind": import_kind,
                "target_module": target,
                "payload": normalized_payload,
            },
        )
        occurrence_key = (
            self._enclosing_qualname,
            import_kind,
            target,
            normalized_fingerprint,
        )
        occurrence_ordinal = self._occurrences.get(occurrence_key, 0)
        self._occurrences[occurrence_key] = occurrence_ordinal + 1
        semantic_payload = {
            "source_module": self._source_module,
            "target_module": target,
            "import_kind": import_kind,
            "enclosing_qualname": self._enclosing_qualname,
            "normalized_import_ast_fingerprint": normalized_fingerprint,
            "equal_ast_occurrence_ordinal": occurrence_ordinal,
        }
        identity = _fingerprint("d4-import-observation-id:v1", semantic_payload)
        self._observations.append(
            ImportObservation(
                **semantic_payload,
                source_path=self._source_path,
                line=int(getattr(node, "lineno", 0)),
                column=int(getattr(node, "col_offset", 0)),
                observation_id=f"import:{identity.removeprefix('sha256:')}",
                observation_fingerprint=_fingerprint(
                    "d4-import-observation:v1", semantic_payload
                ),
            )
        )


def forbidden_d4_observations(
    observations: Iterable[ImportObservation],
) -> tuple[ImportObservation, ...]:
    result = []
    for item in observations:
        source = item.source_module
        target = item.target_module
        forbidden = (
            (
                source.startswith("pulsara_agent.tools")
                and target.startswith(
                    (
                        "pulsara_agent.runtime",
                        "pulsara_agent.host",
                        "pulsara_agent.cli",
                        "pulsara_agent.inspector",
                    )
                )
            )
            or (
                source.startswith("pulsara_agent.capability")
                and target.startswith(("pulsara_agent.runtime", "pulsara_agent.tools"))
            )
            or (
                (
                    _in_module(source, "pulsara_agent.event")
                    or _in_module(source, "pulsara_agent.message")
                )
                and target.startswith("pulsara_agent.replay")
            )
            or (
                _in_module(source, "pulsara_agent.event")
                and target.startswith(
                    (
                        "pulsara_agent.runtime",
                        "pulsara_agent.tools",
                        "pulsara_agent.host",
                    )
                )
            )
            or (
                source.startswith(
                    (
                        "pulsara_agent.storage",
                        "pulsara_agent.graph",
                        "pulsara_agent.memory",
                    )
                )
                and target.startswith("pulsara_agent.runtime.projection_jobs")
            )
            or (
                source.startswith("pulsara_agent.ports")
                and target.startswith(
                    (
                        "pulsara_agent.runtime",
                        "pulsara_agent.tools",
                        "pulsara_agent.host",
                        "pulsara_agent.cli",
                        "pulsara_agent.inspector",
                    )
                )
            )
            or (
                source.startswith("pulsara_agent.projection_jobs")
                and target.startswith("pulsara_agent.runtime.projection_jobs")
            )
            or (
                _in_module(source, "pulsara_agent.primitives.memory_candidate")
                and target.startswith("pulsara_agent.event")
            )
            or (
                source.startswith("pulsara_agent.runtime")
                and target
                in {
                    "pulsara_agent.tools.base",
                    "pulsara_agent.tools.executor",
                }
            )
            or target == "tests"
            or target.startswith("tests.")
        )
        if forbidden:
            result.append(item)
    return tuple(result)


def package_sccs(
    observations: Iterable[ImportObservation],
) -> tuple[tuple[str, ...], ...]:
    graph: dict[str, set[str]] = {}
    for item in observations:
        source = item.source_package
        target = item.target_package
        graph.setdefault(source, set())
        graph.setdefault(target, set())
        if source != target:
            graph[source].add(target)
    return tuple(
        sorted(
            (
                tuple(sorted(component))
                for component in _tarjan(graph)
                if len(component) > 1
            ),
            key=lambda item: item,
        )
    )


def residual_scc_observations(
    observations: Iterable[ImportObservation],
) -> tuple[ImportObservation, ...]:
    items = tuple(observations)
    components = package_sccs(items)
    component_by_package = {
        package: component for component in components for package in component
    }
    return tuple(
        item
        for item in items
        if item.source_package != item.target_package
        and component_by_package.get(item.source_package)
        == component_by_package.get(item.target_package)
        and item.source_package in component_by_package
    )


def observation_set_fingerprint(
    observations: Iterable[ImportObservation],
) -> str:
    return _fingerprint(
        "d4-import-observation-set:v1",
        tuple(
            (item.observation_id, item.observation_fingerprint)
            for item in sorted(observations)
        ),
    )


def _module_for_path(path: Path, source_root: Path) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_import_from(
    source_module: str,
    node: ast.ImportFrom,
    *,
    source_is_package: bool,
) -> str:
    if node.level == 0:
        return node.module or ""
    source_parts = source_module.split(".")
    if not source_is_package:
        source_parts = source_parts[:-1]
    trim = node.level - 1
    if trim:
        source_parts = source_parts[:-trim]
    if node.module:
        source_parts.extend(node.module.split("."))
    return ".".join(source_parts)


def _dynamic_import_call_kind(function: ast.expr) -> str | None:
    if isinstance(function, ast.Name):
        if function.id == "__import__":
            return "dunder_import"
        if function.id == "import_module":
            return "import_module"
    if (
        isinstance(function, ast.Attribute)
        and function.attr == "import_module"
        and isinstance(function.value, ast.Name)
        and function.value.id == "importlib"
    ):
        return "import_module"
    return None


def _literal_dynamic_import_target(
    node: ast.Call,
    *,
    source_module: str,
    source_is_package: bool,
) -> str | None:
    if not node.args:
        return None
    first = node.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return None
    target = first.value
    if not target.startswith("."):
        return target
    package: str | None = None
    if len(node.args) >= 2:
        second = node.args[1]
        if isinstance(second, ast.Constant) and isinstance(second.value, str):
            package = second.value
    if package is None:
        package = (
            source_module if source_is_package else source_module.rpartition(".")[0]
        )
    level = len(target) - len(target.lstrip("."))
    suffix = target[level:]
    package_parts = package.split(".") if package else []
    if level > len(package_parts):
        return None
    base = package_parts[: len(package_parts) - level + 1]
    if suffix:
        base.extend(suffix.split("."))
    return ".".join(base)


def _is_scanned_target(target: str) -> bool:
    return (
        target == "pulsara_agent"
        or target.startswith("pulsara_agent.")
        or target == "tests"
        or target.startswith("tests.")
    )


def _package(module: str) -> str:
    parts = module.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else module


def _in_module(module: str, owner: str) -> bool:
    return module == owner or module.startswith(owner + ".")


def _tarjan(graph: dict[str, set[str]]) -> list[set[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    result: list[set[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for successor in sorted(graph.get(node, ())):
            if successor not in indices:
                visit(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[successor])
        if lowlinks[node] != indices[node]:
            return
        component: set[str] = set()
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.add(member)
            if member == node:
                break
        result.append(component)

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return result


def _fingerprint(namespace: str, payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + sha256(namespace.encode("ascii") + b"\0" + encoded).hexdigest()


__all__ = [
    "ImportObservation",
    "forbidden_d4_observations",
    "observation_set_fingerprint",
    "package_sccs",
    "residual_scc_observations",
    "scan_pulsara_imports",
]
