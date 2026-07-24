"""AST inventory for production EventLog mutation ownership."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


EVENT_ROW_MUTATION_METHODS = frozenset(
    {
        "append",
        "extend",
        "extend_with_materialization_state",
        "write_runtime_projection_checkpoint",
    }
)
MAINTENANCE_METHODS = frozenset(
    {
        "ensure_runtime_session_owner",
        "repair_run_projection",
        "adopt_materialization_account_state_for_test",
    }
)
ALL_MUTATION_METHODS = EVENT_ROW_MUTATION_METHODS | MAINTENANCE_METHODS


@dataclass(frozen=True, slots=True)
class EventLogMutationObservation:
    relative_path: str
    enclosing_class: str | None
    enclosing_function: str
    receiver: str
    method: str
    line: int
    syntax_kind: str

    @property
    def allowlist_key(self) -> tuple[str, str | None, str, str, str]:
        return (
            self.relative_path,
            self.enclosing_class,
            self.enclosing_function,
            self.receiver,
            self.method,
        )


@dataclass(frozen=True, slots=True)
class _CallableScope:
    class_name: str | None
    function_name: str
    node: ast.FunctionDef | ast.AsyncFunctionDef


def scan_production_event_log_mutations(
    package_root: Path,
) -> tuple[EventLogMutationObservation, ...]:
    observations: list[EventLogMutationObservation] = []
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root).as_posix()
        if relative == "event_log" or relative.startswith("event_log/"):
            continue
        observations.extend(
            scan_source_event_log_mutations(
                path.read_text(encoding="utf-8"),
                relative_path=relative,
            )
        )
    return tuple(
        sorted(
            observations,
            key=lambda item: (
                item.relative_path,
                item.line,
                item.syntax_kind,
                item.method,
            ),
        )
    )


def scan_source_event_log_mutations(
    source: str,
    *,
    relative_path: str = "fixture.py",
) -> tuple[EventLogMutationObservation, ...]:
    tree = ast.parse(source, filename=relative_path)
    observations: list[EventLogMutationObservation] = []
    for scope in _callable_scopes(tree):
        scanner = _CallableMutationScanner(
            relative_path=relative_path,
            scope=scope,
        )
        scanner.visit_statements(scope.node.body)
        observations.extend(scanner.observations)
    return tuple(observations)


def _callable_scopes(tree: ast.Module) -> Iterable[_CallableScope]:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield _CallableScope(
                class_name=None,
                function_name=node.name,
                node=node,
            )
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield _CallableScope(
                        class_name=node.name,
                        function_name=child.name,
                        node=child,
                    )


class _CallableMutationScanner:
    def __init__(
        self,
        *,
        relative_path: str,
        scope: _CallableScope,
    ) -> None:
        self.relative_path = relative_path
        self.scope = scope
        self.aliases: dict[str, str] = {}
        self.bound_methods: dict[str, tuple[str, str]] = {}
        self.observations: list[EventLogMutationObservation] = []

    def visit_statements(self, statements: list[ast.stmt]) -> None:
        nodes = tuple(self._current_callable_nodes(statements))
        for node in nodes:
            if isinstance(node, ast.stmt):
                self._learn_assignment(node)
        for node in nodes:
            if isinstance(node, ast.Call):
                self._visit_call(node)

    @staticmethod
    def _current_callable_nodes(
        statements: list[ast.stmt],
    ) -> Iterable[ast.AST]:
        stack: list[ast.AST] = list(reversed(statements))
        while stack:
            node = stack.pop()
            if isinstance(
                node,
                (
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.Lambda,
                    ast.ClassDef,
                ),
            ):
                continue
            yield node
            stack.extend(reversed(tuple(ast.iter_child_nodes(node))))

    def _learn_assignment(self, node: ast.stmt) -> None:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target = node.target
            value = node.value
        else:
            return
        if not isinstance(target, ast.Name):
            return
        if isinstance(value, ast.Attribute) and value.attr in ALL_MUTATION_METHODS:
            receiver = self._normalize_receiver(value.value)
            if self._is_event_log_receiver(receiver, value.attr):
                self.bound_methods[target.id] = (receiver, value.attr)
                self._record(
                    receiver=receiver,
                    method=value.attr,
                    line=value.lineno,
                    syntax_kind="bound_method_escape",
                )
            return
        receiver = self._normalize_receiver(value)
        if self._looks_like_event_log_receiver(receiver):
            self.aliases[target.id] = receiver

    def _visit_call(self, node: ast.Call) -> None:
        function = node.func
        if isinstance(function, ast.Attribute):
            receiver = self._normalize_receiver(function.value)
            method = function.attr
            if method in ALL_MUTATION_METHODS and self._is_event_log_receiver(
                receiver,
                method,
            ):
                self._record(
                    receiver=receiver,
                    method=method,
                    line=node.lineno,
                    syntax_kind="direct_call",
                )
            return
        if isinstance(function, ast.Name) and function.id in self.bound_methods:
            receiver, method = self.bound_methods[function.id]
            self._record(
                receiver=receiver,
                method=method,
                line=node.lineno,
                syntax_kind="bound_method_call",
            )
            return
        getattr_target = self._getattr_target(function)
        if getattr_target is not None:
            receiver, method = getattr_target
            self._record(
                receiver=receiver,
                method=method,
                line=node.lineno,
                syntax_kind="getattr_call",
            )
            return
        self._record_nested_getattr(node)

    def _record_nested_getattr(self, node: ast.Call) -> None:
        target = self._getattr_target(node)
        if target is None:
            return
        receiver, method = target
        self._record(
            receiver=receiver,
            method=method,
            line=node.lineno,
            syntax_kind="getattr_escape",
        )

    def _getattr_target(self, node: ast.AST) -> tuple[str, str] | None:
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value in ALL_MUTATION_METHODS
        ):
            return None
        receiver = self._normalize_receiver(node.args[0])
        method = node.args[1].value
        if not self._is_event_log_receiver(receiver, method):
            return None
        return receiver, method

    def _normalize_receiver(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self._normalize_receiver(node.value)
            return f"{base}.{node.attr}"
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                return f"{node.func.id}()"
            if isinstance(node.func, ast.Attribute):
                return f"{self._normalize_receiver(node.func.value)}.{node.func.attr}()"
        return ast.dump(node, include_attributes=False)

    @staticmethod
    def _looks_like_event_log_receiver(receiver: str) -> bool:
        return (
            receiver in {"event_log", "log", "self.event_log", "self._event_log"}
            or receiver.endswith(".event_log")
            or receiver.endswith("._event_log")
        )

    def _is_event_log_receiver(self, receiver: str, method: str) -> bool:
        if method not in {"append", "extend"}:
            return True
        return self._looks_like_event_log_receiver(receiver)

    def _record(
        self,
        *,
        receiver: str,
        method: str,
        line: int,
        syntax_kind: str,
    ) -> None:
        observation = EventLogMutationObservation(
            relative_path=self.relative_path,
            enclosing_class=self.scope.class_name,
            enclosing_function=self.scope.function_name,
            receiver=receiver,
            method=method,
            line=line,
            syntax_kind=syntax_kind,
        )
        if observation not in self.observations:
            self.observations.append(observation)


__all__ = [
    "EVENT_ROW_MUTATION_METHODS",
    "MAINTENANCE_METHODS",
    "EventLogMutationObservation",
    "scan_production_event_log_mutations",
    "scan_source_event_log_mutations",
]
