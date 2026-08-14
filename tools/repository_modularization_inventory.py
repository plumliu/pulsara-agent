#!/usr/bin/env python3
"""Build the mechanical-equivalence inventory for repository modularization.

The inventory is test evidence only.  It deliberately records syntax and
physical database-call shape; it is not imported by production code and does
not become a persistence registry.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import fields, is_dataclass
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
KERNEL = ROOT / "src/pulsara_agent/conversation_kernel"
FACADE_MODULE = "pulsara_agent.conversation_kernel.repository"
FACADE_PATH = KERNEL / "repository.py"
IMPLEMENTATION_PATH = KERNEL / "_repository"

REPOSITORY_OWNER_CLASSES = {
    "ConversationKernelRepository",
    "_MatchingOperations",
    "_RepositoryKernel",
    "_AuthorityOperations",
    "_ConversationOperations",
    "_ToolOperations",
    "_PlanOperations",
    "_PromptOperations",
    "_SubagentOperations",
    "_ExternalResultOperations",
    "_JobOperations",
    "_MemoryOperations",
}

CLOSED_OWNER_RENAMES = {
    ("ConversationKernelRepository", "_content_from_row"): (
        "_MatchingOperations",
        "_content_from_row",
    ),
    ("ConversationKernelRepository", "_permission_columns"): (
        "_RepositoryKernel",
        "_permission_columns",
    ),
    ("ConversationKernelRepository", "_insert_entry"): (
        "_RepositoryKernel",
        "_insert_entry",
    ),
}

OVERRIDE_SEAMS = (
    "_writer_transaction",
    "_append_events",
    "commit_assistant_message",
    "consume_prepared_prompt_steer",
    "accept_tool_result",
    "accept_root_turn",
    "confirm_root_turn_admission",
    "accept_subagent_turn",
    "confirm_subagent_turn_admission",
    "publish_tool_remote_identity",
    "confirm_tool_remote_identity",
)

SHARED_TRANSACTION_HELPERS = {
    "_writer_transaction": "guarded_writer_mutation",
    "_job_transaction": "guarded_job_mutation",
    "_event_transaction": "job_claim_bootstrap",
}


class _NormalizeClosedOwnerRenames(ast.NodeTransformer):
    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:  # noqa: N802
        node = self.generic_visit(node)
        if isinstance(node.value, ast.Name):
            replacement = CLOSED_OWNER_RENAMES.get((node.value.id, node.attr))
            if replacement is not None:
                owner, attribute = replacement
                return ast.copy_location(
                    ast.Attribute(
                        value=ast.Name(id=owner, ctx=ast.Load()),
                        attr=attribute,
                        ctx=node.ctx,
                    ),
                    node,
                )
        return node


def _source_paths() -> tuple[Path, ...]:
    paths = [FACADE_PATH]
    if IMPLEMENTATION_PATH.exists():
        paths.extend(sorted(IMPLEMENTATION_PATH.glob("*.py")))
    return tuple(path for path in paths if path.exists())


def _trees() -> tuple[tuple[Path, ast.Module], ...]:
    return tuple(
        (
            path,
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )
        for path in _source_paths()
    )


def _normalized_dump(node: ast.AST) -> str:
    node = ast.fix_missing_locations(node)
    normalized = _NormalizeClosedOwnerRenames().visit(
        ast.fix_missing_locations(ast.parse(ast.unparse(node)).body[0])
    )
    return ast.dump(normalized, include_attributes=False)


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_node(node: ast.AST) -> str:
    return _digest_text(_normalized_dump(node))


def _definition_nodes() -> tuple[
    dict[str, tuple[Path, ast.ClassDef]],
    dict[str, tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]],
    dict[str, tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]],
]:
    classes: dict[str, tuple[Path, ast.ClassDef]] = {}
    functions: dict[str, tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    methods: dict[str, tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for path, tree in _trees():
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                if node.name in REPOSITORY_OWNER_CLASSES:
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            if child.name in methods:
                                raise RuntimeError(
                                    f"repository method is defined twice: {child.name}"
                                )
                            methods[child.name] = (path, child)
                elif not node.name.startswith("_Repository") and not node.name.endswith(
                    "Operations"
                ):
                    if node.name in classes:
                        raise RuntimeError(
                            f"repository top-level class is defined twice: {node.name}"
                        )
                    classes[node.name] = (path, node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in functions:
                    raise RuntimeError(
                        f"repository top-level function is defined twice: {node.name}"
                    )
                functions[node.name] = (path, node)
    return classes, functions, methods


def _decorator_kind(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    names = {
        item.id
        for item in node.decorator_list
        if isinstance(item, ast.Name)
    }
    if "staticmethod" in names:
        return "staticmethod"
    if "classmethod" in names:
        return "classmethod"
    if "property" in names:
        return "property"
    return "instance"


def _signature_dump(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    clone = ast.FunctionDef(
        name=node.name,
        args=node.args,
        body=[ast.Pass()],
        decorator_list=node.decorator_list,
        returns=node.returns,
        type_comment=node.type_comment,
        type_params=getattr(node, "type_params", []),
    )
    return _normalized_dump(clone)


def _attribute_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _database_calls(
    definitions: Iterable[
        tuple[str, tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]]
    ],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    interesting = {
        "connection",
        "execute",
        "executemany",
        "fetchone",
        "fetchall",
    }
    for owner, (path, definition) in definitions:
        calls = sorted(
            (
                node
                for node in ast.walk(definition)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in interesting
            ),
            key=lambda node: (node.lineno, node.col_offset),
        )
        for ordinal, call in enumerate(calls):
            records.append(
                {
                    "owner": owner,
                    "ordinal": ordinal,
                    "kind": call.func.attr,
                    "call_target": _attribute_chain(call.func),
                    "call_digest": _digest_node(call),
                    "argument_digest": _digest_text(
                        ast.dump(
                            _NormalizeClosedOwnerRenames().visit(
                                ast.Tuple(
                                    elts=[*call.args, *(item.value for item in call.keywords)],
                                    ctx=ast.Load(),
                                )
                            ),
                            include_attributes=False,
                        )
                    ),
                    "source_module": str(path.relative_to(ROOT)),
                }
            )
    return records


def _physical_checkouts(
    methods: dict[str, tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for name, (path, method) in sorted(methods.items()):
        connection_calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "connection"
            and (_attribute_chain(node.func) or "").endswith("._provider.connection")
        ]
        for ordinal, call in enumerate(
            sorted(connection_calls, key=lambda node: (node.lineno, node.col_offset))
        ):
            classification = SHARED_TRANSACTION_HELPERS.get(name, "direct_operation")
            if name in {"acquire_host_writer", "renew_host_writer"}:
                classification = "writer_bootstrap_or_renew"
            records.append(
                {
                    "owner": name,
                    "ordinal": ordinal,
                    "classification": classification,
                    "call_digest": _digest_node(call),
                    "source_module": str(path.relative_to(ROOT)),
                }
            )
    return records


def _all_ordered() -> list[str]:
    for _path, tree in _trees():
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(item, ast.Name) and item.id == "__all__" for item in targets):
                continue
            value = node.value
            if not isinstance(value, (ast.List, ast.Tuple)):
                raise RuntimeError("repository __all__ must be a literal sequence")
            return [ast.literal_eval(item) for item in value.elts]
    raise RuntimeError("repository __all__ is absent")


def _observed_imports() -> tuple[str, ...]:
    result: set[str] = set()
    for root_name in ("src", "tests", "tools"):
        for path in (ROOT / root_name).rglob("*.py"):
            if path == FACADE_PATH or IMPLEMENTATION_PATH in path.parents:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module == FACADE_MODULE
                ):
                    result.update(item.name for item in node.names)
    return tuple(sorted(result))


def _object_kind(value: object) -> str:
    if inspect.isclass(value):
        return "class"
    if inspect.isfunction(value):
        return "function"
    return f"instance:{type(value).__module__}.{type(value).__qualname__}"


def _runtime_contracts(symbols: Iterable[str]) -> dict[str, object]:
    importlib.invalidate_caches()
    module = importlib.import_module(FACADE_MODULE)
    observed: dict[str, object] = {}
    for name in symbols:
        value = getattr(module, name)
        observed[name] = {
            "kind": _object_kind(value),
            "module": getattr(value, "__module__", None),
            "qualname": getattr(value, "__qualname__", None),
        }
    classes, _functions, methods = _definition_nodes()
    dataclasses: dict[str, object] = {}
    exceptions: dict[str, object] = {}
    internal_contracts = importlib.import_module(
        "pulsara_agent.conversation_kernel._repository.contracts"
    ) if IMPLEMENTATION_PATH.exists() else None
    for name in classes:
        value = getattr(module, name, None)
        if value is None and internal_contracts is not None:
            value = getattr(internal_contracts, name)
        assert value is not None
        if is_dataclass(value):
            parameters = value.__dataclass_params__
            dataclasses[name] = {
                "fields": [item.name for item in fields(value)],
                "frozen": parameters.frozen,
                "slots": hasattr(value, "__slots__"),
            }
        if inspect.isclass(value) and issubclass(value, BaseException):
            exceptions[name] = [
                f"{base.__module__}.{base.__qualname__}" for base in value.__bases__
            ]
    repository = module.ConversationKernelRepository
    method_runtime: dict[str, object] = {}
    for name in sorted(methods):
        descriptor = inspect.getattr_static(repository, name)
        if isinstance(descriptor, (staticmethod, classmethod)):
            function = descriptor.__func__
        elif isinstance(descriptor, property):
            function = descriptor.fget
            assert function is not None
        else:
            function = descriptor
        method_runtime[name] = {
            "signature": str(inspect.signature(function)),
            "is_async": inspect.iscoroutinefunction(function),
        }
    return {
        "observed_symbols": observed,
        "owned_observed_symbols": sorted(
            name
            for name, item in observed.items()
            if item["module"] == FACADE_MODULE
            and item["kind"] in {"class", "function"}
        ),
        "constructor_signature": str(inspect.signature(repository)),
        "dataclasses": dataclasses,
        "exceptions": exceptions,
        "methods": method_runtime,
    }


def _pytest_node_ids() -> list[str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(
        line.strip()
        for line in completed.stdout.splitlines()
        if "::" in line and not line.startswith(("=", "<"))
    )


def build_inventory(*, include_pytest_nodes: bool) -> dict[str, Any]:
    classes, functions, methods = _definition_nodes()
    definitions = sorted([*functions.items(), *methods.items()])
    observed = _observed_imports()
    method_manifest = {
        name: {
            "ast_digest": _digest_node(node),
            "signature_ast_digest": _digest_text(_signature_dump(node)),
            "is_async": isinstance(node, ast.AsyncFunctionDef),
            "decorator_kind": _decorator_kind(node),
            "stable_literal_digest": _digest_text(
                json.dumps(
                    sorted(
                        value.value
                        for value in ast.walk(node)
                        if isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and value.value.startswith("pulsara:")
                    ),
                    separators=(",", ":"),
                )
            ),
        }
        for name, (_path, node) in sorted(methods.items())
    }
    function_manifest = {
        name: {
            "ast_digest": _digest_node(node),
            "signature_ast_digest": _digest_text(_signature_dump(node)),
            "is_async": isinstance(node, ast.AsyncFunctionDef),
            "stable_literal_digest": _digest_text(
                json.dumps(
                    sorted(
                        value.value
                        for value in ast.walk(node)
                        if isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and value.value.startswith("pulsara:")
                    ),
                    separators=(",", ":"),
                )
            ),
        }
        for name, (_path, node) in sorted(functions.items())
    }
    class_qualified_calls = sorted(
        {
            _attribute_chain(node.func)
            for _name, (_path, definition) in definitions
            for node in ast.walk(definition)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id
            in {"ConversationKernelRepository", "_RepositoryKernel", "_MatchingOperations"}
        }
        - {None}
    )
    result: dict[str, Any] = {
        "schema_version": "repository-modularization-baseline.v1",
        "checkpoint_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "repository_sha256": hashlib.sha256(FACADE_PATH.read_bytes()).hexdigest(),
        "source_modules": [str(path.relative_to(ROOT)) for path in _source_paths()],
        "all": _all_ordered(),
        "observed_imports": list(observed),
        "top_level_classes": sorted(classes),
        "top_level_functions": function_manifest,
        "methods": method_manifest,
        "database_calls": _database_calls(definitions),
        "physical_checkouts": _physical_checkouts(methods),
        "class_qualified_calls": class_qualified_calls,
        "closed_owner_renames": {
            f"{owner}.{name}": f"{replacement[0]}.{replacement[1]}"
            for (owner, name), replacement in CLOSED_OWNER_RENAMES.items()
        },
        "override_seams": list(OVERRIDE_SEAMS),
        "runtime": _runtime_contracts(observed),
    }
    if include_pytest_nodes:
        result["pytest_node_ids"] = _pytest_node_ids()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--without-pytest-nodes", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            build_inventory(include_pytest_nodes=not args.without_pytest_nodes),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
