from __future__ import annotations

import ast
import inspect
from pathlib import Path
import re

from pulsara_agent.runtime.wiring import build_durable_runtime_wiring


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "pulsara_agent"
MIGRATION_SQL_ROOT = SOURCE_ROOT / "storage" / "migrations" / "sql"

_CONNECTION_CALL_ALLOWLIST = {
    "storage/postgres_endpoint.py",
    "storage/postgres_connection_provider.py",
}
_RAW_DSN_FIELD_ALLOWLIST = {
    "cli.py",
    "settings.py",
    "storage/migrations/runner.py",
    "storage/postgres_connection_provider.py",
    "storage/postgres_endpoint.py",
    "storage/schema_verification_service.py",
    "host/core.py",
}
_DDL_PREFIX = re.compile(
    r"^\s*(CREATE|ALTER|DROP|TRUNCATE|REINDEX|COMMENT\s+ON)\b",
    re.IGNORECASE,
)


def _python_sources():
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        yield path, path.relative_to(SOURCE_ROOT).as_posix(), path.read_text(
            encoding="utf-8"
        )


def test_schema_ddl_has_no_python_execution_owner() -> None:
    violations: list[str] = []
    for path, relative, source in _python_sources():
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if not (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"execute", "executemany"}
            ):
                continue
            statement = node.args[0]
            if isinstance(statement, ast.Constant) and isinstance(
                statement.value, str
            ) and _DDL_PREFIX.match(statement.value):
                violations.append(f"{relative}:{node.lineno}")
    assert violations == []


def test_runtime_acl_owner_is_the_typed_grant_executor_only() -> None:
    findings = []
    for path, relative, source in _python_sources():
        if re.search(r"\b(?:GRANT|REVOKE)\b", source):
            findings.append(relative)
    assert findings == ["storage/migrations/grants.py"]
    grants = (SOURCE_ROOT / findings[0]).read_text(encoding="utf-8")
    assert "REVOKE" not in grants
    assert "sql.Identifier(runtime_role)" in grants


def test_production_physical_connections_have_one_owner() -> None:
    violations: list[str] = []
    for path, relative, source in _python_sources():
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            dotted = _dotted_name(node.func)
            if dotted in {"psycopg.connect", "ConnectionPool"} and (
                relative not in _CONNECTION_CALL_ALLOWLIST
            ):
                violations.append(f"{relative}:{node.lineno}:{dotted}")
    assert violations == []


def test_production_adapter_constructors_have_no_raw_dsn_authority() -> None:
    violations: list[str] = []
    forbidden = {"dsn", "postgres_dsn", "conninfo"}
    for path, relative, source in _python_sources():
        if relative in _RAW_DSN_FIELD_ALLOWLIST:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "__init__":
                names = {
                    argument.arg
                    for argument in (*node.args.args, *node.args.kwonlyargs)
                }
                if names & forbidden:
                    violations.append(f"{relative}:{node.lineno}:{sorted(names & forbidden)}")
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id in forbidden:
                    violations.append(f"{relative}:{node.lineno}:{node.target.id}")
    assert violations == []


def test_old_schema_bootstrap_symbols_are_physically_absent() -> None:
    assert not (SOURCE_ROOT / "storage" / "postgres_schema.py").exists()
    assert not (SOURCE_ROOT / "storage" / "memory_schema.py").exists()
    violations = []
    for _path, relative, source in _python_sources():
        if "ensure_schema" in source or "_SCHEMA_SQL" in source:
            violations.append(relative)
    assert violations == []


def test_baseline_sql_is_forward_only_and_extension_is_unique() -> None:
    resources = sorted(MIGRATION_SQL_ROOT.glob("*.sql"))
    assert [path.name for path in resources] == [
        "0000_schema_migration_ledger.sql",
        "0001_pgvector_extension.sql",
        "0002_runtime_truth_baseline.sql",
        "0003_memory_substrate_baseline.sql",
        "0004_memory_governance_baseline.sql",
    ]
    create_extension_owners = []
    for path in resources:
        sql_text = path.read_text(encoding="utf-8")
        upper = sql_text.upper()
        if "CREATE EXTENSION" in upper:
            create_extension_owners.append(path.name)
        if path.name != "0001_pgvector_extension.sql":
            assert "IF NOT EXISTS" not in upper
        assert " ADD COLUMN " not in upper
        assert " DROP COLUMN " not in upper
        assert " ALTER COLUMN " not in upper
        assert re.search(r"\bUPDATE\b", upper) is None
        assert re.search(r"\bDELETE\s+FROM\b", upper) is None
        assert "CREATE INDEX CONCURRENTLY" not in upper
    assert create_extension_owners == ["0001_pgvector_extension.sql"]


def test_durable_runtime_wiring_requires_verified_access_lease() -> None:
    parameter = inspect.signature(build_durable_runtime_wiring).parameters[
        "postgres_access_lease"
    ]
    assert parameter.default is inspect.Parameter.empty


def _dotted_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""
