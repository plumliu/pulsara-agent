"""Real-provider discovery dogfood for the bundled Plugin installer guidance.

Each positive test-vault package is handled in an isolated user home.  The
human prompt asks for an ordinary local Plugin installation without naming a
Skill.  Evidence is accepted only when the real provider discovers and reads
the bundled guidance before issuing any terminal command, then uses the
installed global ``pulsara`` launcher to validate, add, and inspect the package.

The trace retains provider-visible inputs, provider output, canonical tool
rows, CLI stdout/stderr, transcript rows, and independently inspected managed
state.  The exact non-empty ``PULSARA_API_KEY`` is the only scrubbed value.
"""

from __future__ import annotations

from pulsara_agent.llm.input import PromptContent
import argparse
import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
from typing import Mapping

from pulsara_agent.capability.pulsara_home import resolve_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.plugins.contracts import (
    InspectLocalPluginsRequest,
    PluginInspectionDisposition,
    PluginInspectionOutcome,
)
from pulsara_agent.plugins.management import PluginManagementService
from pulsara_agent.ports.provider_stream import (
    ProviderModelExecutionFailed,
    ProviderModelOutputIncomplete,
)
from pulsara_agent.process_api_key_boundary import ProcessApiKeyBoundary
from pulsara_agent.settings import PulsaraSettings, load_env_file
from pulsara_agent.workspace_identity import HostWorkspaceInput

from run_round9_2_hook_dogfood import (
    _ProviderTraceRecorder,
    _TracingModel,
    _create_database,
    _drop_database,
    _runtime_settings,
    _tool_rows,
    _transcript_rows,
)


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_VAULT_ROOT = Path(
    "/Users/plumliu/Desktop/python_workspace/pulsara_plugin_test_vault"
).resolve()
_POSITIVE_NAMES = (
    "elements-of-style",
    "jumpcloud-admin",
    "fdeops",
    "spar",
    "one",
)
_GUIDANCE_PATH = (
    _REPOSITORY_ROOT
    / "src/pulsara_agent/bundled_skills/pulsara-plugin-installer/SKILL.md"
).resolve()
_TRACE_PATH = Path(
    "benchmarks/suites/core/v1/"
    "round9_3_plugin_installer_discovery_trace.json"
)
_API_KEY_REPLACEMENT = "<PULSARA_API_KEY>"
_FORBIDDEN_PROMPT_TEXT = (
    "pulsara-plugin-installer",
    "plugin installer skill",
    "$pulsara-",
)


class DogfoodCommandFailed(RuntimeError):
    def __init__(self, record: dict[str, object]) -> None:
        self.record = record
        super().__init__(
            f"command failed with exit code {record.get('returncode')}: "
            f"{record.get('argv')}"
        )


class FixtureSemanticFailure(RuntimeError):
    def __init__(self, checks: dict[str, object]) -> None:
        self.checks = checks
        failed = sorted(
            key for key, value in checks.items() if isinstance(value, bool) and not value
        )
        super().__init__("fixture semantic checks failed: " + ", ".join(failed))


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return os.fspath(value)
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    return repr(value)


def _scrub_exact(value: object, secret: str) -> object:
    if isinstance(value, str):
        return value.replace(secret, _API_KEY_REPLACEMENT) if secret else value
    if isinstance(value, dict):
        return {
            str(_scrub_exact(key, secret)): _scrub_exact(item, secret)
            for key, item in value.items()
        }
    if isinstance(value, tuple | list):
        return [_scrub_exact(item, secret) for item in value]
    return value


def _subprocess_environment(secret: str) -> dict[str, str]:
    values = dict(os.environ)
    values.pop("PULSARA_API_KEY", None)
    if secret:
        for name, value in tuple(values.items()):
            if secret in value:
                values.pop(name, None)
    values.pop("PYTHONPATH", None)
    return values


def _run_command(
    argv: list[str], *, cwd: Path, env: Mapping[str, str]
) -> dict[str, object]:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=dict(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    record: dict[str, object] = {
        "argv": argv,
        "cwd": os.fspath(cwd),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    if completed.returncode != 0:
        raise DogfoodCommandFailed(record)
    return record


def _build_isolated_launcher(root: Path, secret: str) -> tuple[Path, dict[str, object]]:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv executable is unavailable")
    build_root = root / "distribution"
    sdist_dir = build_root / "sdist"
    wheel_dir = build_root / "wheel"
    tool_dir = build_root / "tool-dir"
    bin_dir = build_root / "bin"
    non_source_cwd = build_root / "non-source-cwd"
    for path in (sdist_dir, wheel_dir, tool_dir, bin_dir, non_source_cwd):
        path.mkdir(parents=True)
    env = _subprocess_environment(secret)
    records: list[dict[str, object]] = []
    records.append(
        _run_command(
            [uv, "build", "--sdist", "--out-dir", os.fspath(sdist_dir)],
            cwd=_REPOSITORY_ROOT,
            env=env,
        )
    )
    sdists = tuple(sorted(sdist_dir.glob("*.tar.gz")))
    if len(sdists) != 1:
        raise RuntimeError(f"expected one sdist, observed {sdists!r}")
    records.append(
        _run_command(
            [
                uv,
                "build",
                "--wheel",
                "--out-dir",
                os.fspath(wheel_dir),
                os.fspath(sdists[0]),
            ],
            cwd=non_source_cwd,
            env=env,
        )
    )
    wheels = tuple(sorted(wheel_dir.glob("*.whl")))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one wheel, observed {wheels!r}")
    tool_env = {
        **env,
        "UV_TOOL_DIR": os.fspath(tool_dir),
        "UV_TOOL_BIN_DIR": os.fspath(bin_dir),
    }
    records.append(
        _run_command(
            [uv, "tool", "install", os.fspath(wheels[0])],
            cwd=non_source_cwd,
            env=tool_env,
        )
    )
    launcher = bin_dir / "pulsara"
    if not launcher.is_file():
        raise RuntimeError("isolated pulsara launcher was not installed")
    smoke_version = _run_command(
        [os.fspath(launcher), "--version"], cwd=non_source_cwd, env=env
    )
    smoke_plugins = _run_command(
        [os.fspath(launcher), "plugins", "--help"],
        cwd=non_source_cwd,
        env=env,
    )
    return bin_dir, {
        "sdist": os.fspath(sdists[0]),
        "wheel": os.fspath(wheels[0]),
        "tool_dir": os.fspath(tool_dir),
        "bin_dir": os.fspath(bin_dir),
        "non_source_cwd": os.fspath(non_source_cwd),
        "commands": records,
        "smoke_version": smoke_version,
        "smoke_plugins": smoke_plugins,
    }


def _fixture_source(name: str) -> Path:
    source = (_VAULT_ROOT / "positive" / name).resolve(strict=True)
    if source.parent != (_VAULT_ROOT / "positive").resolve(strict=True):
        raise RuntimeError(f"positive fixture escaped the vault: {source}")
    return source


def _manifest_identity(source: Path) -> tuple[str, str]:
    value = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    plugin_id = value.get("name")
    version = value.get("version")
    if not isinstance(plugin_id, str) or not isinstance(version, str):
        raise RuntimeError(f"fixture manifest identity is invalid: {source}")
    return plugin_id, version


def _natural_user_prompt(source: Path) -> str:
    prompt = (
        "我需要把下面这个本地目录中的插件安装到 Pulsara 的当前用户范围：\n"
        f"{source}\n\n"
        "请先阅读当前会话提供的最相关操作指南，弄清楚这个 Pulsara 安装版的"
        "正式本地插件流程；在读完之前不要运行任何终端命令。随后使用系统已安装的"
        "全局 pulsara CLI 真实完成能力确认、严格验证、安装和安装后状态检查。"
        "如果源格式不兼容，请先判断能否在不改变任何行为的前提下精确处理；不能"
        "精确处理就停止，并且不要安装部分功能。不要启用或运行插件组件。最后用"
        "简短中文报告实际命令结果，并说明遇到格式不兼容时继续或停止的边界。"
    )
    lowered = prompt.lower()
    forbidden = [value for value in _FORBIDDEN_PROMPT_TEXT if value in lowered]
    if forbidden:
        raise RuntimeError(f"natural user prompt names forbidden guidance: {forbidden}")
    return prompt


def _tool_arguments(row: Mapping[str, object]) -> dict[str, object]:
    value = row.get("tool_arguments")
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(decoded, dict):
            return {str(key): item for key, item in decoded.items()}
    return {}


def _inspection_state(
    *, home: Path, workspace: Path, boundary: ProcessApiKeyBoundary
) -> dict[str, object]:
    service = PluginManagementService(
        api_key_boundary=boundary,
        pulsara_home_resolution=resolve_pulsara_home(os.fspath(home)),
    )
    value = service.inspect_local_plugins(
        InspectLocalPluginsRequest(
            monotonic() + 120,
            workspace_root=workspace.resolve(strict=True),
        )
    )
    if not isinstance(value, PluginInspectionOutcome):
        return {"type": type(value).__name__, "value": _jsonable(value)}
    try:
        return {
            "type": type(value).__name__,
            "disposition": value.disposition.value,
            "instances": [
                {
                    "plugin_id": item.identity.plugin_id,
                    "scope": item.identity.scope.value,
                    "package_install_id": item.package_install_id,
                    "enabled": item.enabled,
                    "package_root": os.fspath(item.package_root),
                    "data_root": os.fspath(item.data_root),
                    "manifest_version": item.summary.manifest.version,
                    "skill_names": [skill.name for skill in item.summary.skills.skills],
                    "mcp_server_ids": [
                        server.local_server_id
                        for server in item.summary.mcp.mcp_servers
                    ],
                    "hook_definition_count": len(
                        item.summary.hooks.hook_definitions
                    ),
                }
                for item in value.instances
            ],
            "versions": [
                {
                    "plugin_id": item.identity.plugin_id,
                    "scope": item.identity.scope.value,
                    "package_install_id": item.package_install_id,
                    "referenced": item.referenced,
                    "in_use": item.in_use,
                }
                for item in value.versions
            ],
        }
    finally:
        value.close()


def _semantic_checks(
    *,
    source: Path,
    plugin_id: str,
    version: str,
    prompt: str,
    turn_result: Mapping[str, object],
    rows: list[dict[str, object]],
    inspection: Mapping[str, object],
) -> dict[str, object]:
    read_rows: list[tuple[int, dict[str, object]]] = []
    terminal_rows: list[tuple[int, dict[str, object]]] = []
    for index, row in enumerate(rows):
        name = row.get("tool_name")
        arguments = _tool_arguments(row)
        if name == "read_file" and arguments.get("path") == os.fspath(_GUIDANCE_PATH):
            read_rows.append((index, row))
        if name == "terminal":
            terminal_rows.append((index, row))
    successful_reads = [
        (index, row)
        for index, row in read_rows
        if row.get("result_state") == "SUCCESS"
        and "Strict-First Workflow" in str(row.get("result_text") or "")
        and "If validation is `VALID`" in str(row.get("result_text") or "")
    ]
    first_read_index = successful_reads[0][0] if successful_reads else None
    terminal_after_read = bool(terminal_rows) and first_read_index is not None and all(
        index > first_read_index for index, _row in terminal_rows
    )
    terminal_in_later_model_entry = bool(terminal_rows) and bool(successful_reads) and all(
        int(row.get("entry_sequence") or -1)
        > int(successful_reads[0][1].get("entry_sequence") or -1)
        for _index, row in terminal_rows
    )
    commands = [
        str(_tool_arguments(row).get("command") or "")
        for _index, row in terminal_rows
    ]
    joined_commands = "\n".join(commands)
    terminal_results = "\n".join(
        str(row.get("result_text") or "") for _index, row in terminal_rows
    )
    instances_value = inspection.get("instances")
    instances = instances_value if isinstance(instances_value, list) else []
    expected_instances = [
        item
        for item in instances
        if isinstance(item, dict)
        and item.get("plugin_id") == plugin_id
        and item.get("scope") == "USER"
        and item.get("manifest_version") == version
    ]
    final_text = str(turn_result.get("final_text") or "")
    lowered_prompt = prompt.lower()
    return {
        "user_prompt_omits_guidance_name": not any(
            value in lowered_prompt for value in _FORBIDDEN_PROMPT_TEXT
        ),
        "guidance_read_successfully": bool(successful_reads),
        "guidance_read_before_every_terminal_call": terminal_after_read,
        "terminal_generated_after_guidance_tool_result": terminal_in_later_model_entry,
        "global_plugins_surface_confirmed": "pulsara plugins --help" in joined_commands,
        "strict_validator_invoked": (
            "pulsara plugins validate" in joined_commands
            and os.fspath(source) in joined_commands
        ),
        "user_scope_add_invoked": (
            "pulsara plugins add" in joined_commands
            and "--scope user" in joined_commands
            and os.fspath(source) in joined_commands
        ),
        "post_install_inspection_invoked": (
            "pulsara plugins list" in joined_commands
            or "pulsara plugins doctor" in joined_commands
        ),
        "plugin_not_enabled_by_model": "pulsara plugins enable" not in joined_commands,
        "validator_reported_valid": "VALID" in terminal_results,
        "installer_reported_installed": "INSTALLED" in terminal_results,
        "managed_state_complete": (
            inspection.get("disposition")
            == PluginInspectionDisposition.COMPLETE.value
        ),
        "expected_disabled_instance_installed": (
            len(expected_instances) == 1
            and expected_instances[0].get("enabled") is False
        ),
        "model_reported_real_result": bool(final_text.strip()),
        "model_explained_incompatibility_boundary": (
            "不兼容" in final_text
            and ("转换" in final_text or "处理" in final_text)
            and ("停止" in final_text or "不安装" in final_text)
        ),
        "commands": commands,
        "terminal_result_text": terminal_results,
        "successful_guidance_read_count": len(successful_reads),
    }


def _classification(exc: BaseException) -> str:
    if isinstance(exc, ProviderModelExecutionFailed | ProviderModelOutputIncomplete):
        return "EXTERNAL_AVAILABILITY"
    if isinstance(exc, FixtureSemanticFailure):
        return "MODEL_OR_PRODUCT_SEMANTIC_FAILURE"
    if isinstance(exc, DogfoodCommandFailed):
        return "PACKAGING_OR_LAUNCHER_FAILURE"
    return "RUNTIME_DEFECT"


async def _run_fixture(
    *,
    settings: PulsaraSettings,
    source: Path,
    root: Path,
    launcher_bin: Path,
) -> dict[str, object]:
    import pulsara_agent.conversation_kernel.host as host_module

    plugin_id, version = _manifest_identity(source)
    fixture_root = root / plugin_id
    home = fixture_root / "home"
    product_home = fixture_root / "pulsara-home"
    workspace = fixture_root / "workspace"
    for path in (home, product_home, workspace):
        path.mkdir(parents=True)
    prompt = _natural_user_prompt(source)
    boundary = ProcessApiKeyBoundary()
    recorder = _ProviderTraceRecorder()
    original_model = host_module.DirectKernelModelPort
    host_module.DirectKernelModelPort = lambda **kwargs: _TracingModel(  # type: ignore[assignment]
        original_model(**kwargs), recorder
    )
    core: KernelHostCore | None = None
    session = None
    previous_environment = {
        name: os.environ.get(name)
        for name in (
            "HOME",
            "PULSARA_HOME",
            "PULSARA_TERMINAL_ENV_PASSTHROUGH_NAMES",
            "PULSARA_TERMINAL_EXTRA_PATH_PREPENDS",
            "PULSARA_TERMINAL_SHELL_SNAPSHOT",
            "PULSARA_TERMINAL_VENV_OVERLAY",
        )
    }
    try:
        os.environ["HOME"] = os.fspath(home)
        os.environ["PULSARA_HOME"] = os.fspath(product_home)
        os.environ["PULSARA_TERMINAL_ENV_PASSTHROUGH_NAMES"] = "PULSARA_HOME"
        os.environ["PULSARA_TERMINAL_EXTRA_PATH_PREPENDS"] = os.fspath(launcher_bin)
        os.environ["PULSARA_TERMINAL_SHELL_SNAPSHOT"] = "false"
        os.environ["PULSARA_TERMINAL_VENV_OVERLAY"] = "false"
        core = KernelHostCore.production(settings=settings, api_key_boundary=boundary)
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace,
            ),
            system_prompt=(
                "You are performing a real Pulsara local-package operation. Use the "
                "provider-visible runtime catalogs to discover applicable guidance, "
                "read that guidance with real tools before acting, and execute real "
                "commands rather than simulating them. Preserve the human's requested "
                "scope and do not enable or execute installed package components."
            ),
        )
        turn = await session.run_turn(
            PromptContent.text(prompt),
            command_id=f"command:round9-3:installer-discovery:{plugin_id}",
        )
        rows = _tool_rows(session)
        transcript = _transcript_rows(session)
        inspection = _inspection_state(
            home=product_home,
            workspace=workspace,
            boundary=boundary,
        )
        turn_public = _jsonable(turn)
        if not isinstance(turn_public, dict):
            raise RuntimeError("kernel turn result did not serialize to an object")
        checks = _semantic_checks(
            source=source,
            plugin_id=plugin_id,
            version=version,
            prompt=prompt,
            turn_result=turn_public,
            rows=rows,
            inspection=inspection,
        )
        boolean_checks = {
            key: value for key, value in checks.items() if isinstance(value, bool)
        }
        if not all(boolean_checks.values()):
            raise FixtureSemanticFailure(checks)
        return {
            "status": "passed",
            "plugin_id": plugin_id,
            "version": version,
            "source": os.fspath(source),
            "user_prompt": prompt,
            "turn_result": turn_public,
            "checks": checks,
            "inspection": inspection,
            "provider_requests": recorder.requests,
            "provider_stream": recorder.stream,
            "canonical_tool_rows": rows,
            "canonical_transcript": transcript,
        }
    except BaseException as exc:
        evidence: dict[str, object] = {
            "status": "failed",
            "failure_classification": _classification(exc),
            "failure_type": type(exc).__name__,
            "failure_message": str(exc),
            "failure_traceback": traceback.format_exc(limit=64),
            "plugin_id": plugin_id,
            "version": version,
            "source": os.fspath(source),
            "user_prompt": prompt,
            "provider_requests": recorder.requests,
            "provider_stream": recorder.stream,
        }
        if isinstance(exc, FixtureSemanticFailure):
            evidence["checks"] = exc.checks
        if session is not None:
            try:
                evidence["canonical_tool_rows"] = _tool_rows(session)
            except BaseException as observation_exc:
                evidence["tool_observation_failure"] = {
                    "type": type(observation_exc).__name__,
                    "message": str(observation_exc),
                }
            try:
                evidence["canonical_transcript"] = _transcript_rows(session)
            except BaseException as observation_exc:
                evidence["transcript_observation_failure"] = {
                    "type": type(observation_exc).__name__,
                    "message": str(observation_exc),
                }
        try:
            evidence["inspection"] = _inspection_state(
                home=product_home,
                workspace=workspace,
                boundary=boundary,
            )
        except BaseException as observation_exc:
            evidence["inspection_failure"] = {
                "type": type(observation_exc).__name__,
                "message": str(observation_exc),
            }
        return evidence
    finally:
        if core is not None:
            await core.shutdown()
        host_module.DirectKernelModelPort = original_model
        for name, value in previous_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def _run_matrix(
    *, settings: PulsaraSettings, root: Path, launcher_bin: Path
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for name in _POSITIVE_NAMES:
        results.append(
            await _run_fixture(
                settings=settings,
                source=_fixture_source(name),
                root=root,
                launcher_bin=launcher_bin,
            )
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--trace", type=Path, default=_TRACE_PATH)
    args = parser.parse_args()

    stage = "ENVIRONMENT_LOAD"
    secret = ""
    admin_root_dsn: str | None = None
    database_name: str | None = None
    report: dict[str, object]
    try:
        load_env_file(args.env_file, override=False)
        settings = PulsaraSettings.from_env()
        secret = settings.llm.api_key
        with TemporaryDirectory(prefix="pulsara-round9-3-installer-discovery-") as raw:
            root = Path(raw).resolve(strict=True)
            stage = "ISOLATED_DISTRIBUTION_BUILD"
            launcher_bin, packaging = _build_isolated_launcher(root, secret)
            stage = "DATABASE_SETUP"
            (
                admin_root_dsn,
                database_name,
                runtime_dsn,
                database_evidence,
            ) = _create_database(settings)
            runtime_settings = _runtime_settings(args.env_file, runtime_dsn)
            stage = "REAL_PROVIDER_POSITIVE_MATRIX"
            fixtures = asyncio.run(
                _run_matrix(
                    settings=runtime_settings,
                    root=root / "fixtures",
                    launcher_bin=launcher_bin,
                )
            )
            passed = [item for item in fixtures if item.get("status") == "passed"]
            report = {
                "schema_version": (
                    "round9-3-plugin-installer-natural-discovery-dogfood.v1"
                ),
                "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                "status": "passed" if len(passed) == len(fixtures) else "failed",
                "natural_user_prompt_names_installer_skill": False,
                "positive_fixture_order": list(_POSITIVE_NAMES),
                "packaging": packaging,
                "database": database_evidence,
                "fixtures": fixtures,
                "pulsara_api_key_recorded": False,
            }
    except BaseException as exc:
        failure: dict[str, object] = {
            "schema_version": (
                "round9-3-plugin-installer-natural-discovery-dogfood.v1"
            ),
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "failed",
            "failure_stage": stage,
            "failure_classification": _classification(exc),
            "failure_type": type(exc).__name__,
            "failure_message": str(exc),
            "failure_traceback": traceback.format_exc(limit=64),
            "pulsara_api_key_recorded": False,
        }
        if isinstance(exc, DogfoodCommandFailed):
            failure["failed_command"] = exc.record
        report = failure
    finally:
        if admin_root_dsn is not None and database_name is not None:
            try:
                _drop_database(admin_root_dsn, database_name)
            except BaseException as cleanup_exc:
                if report.get("status") == "passed":
                    report = {
                        **report,
                        "status": "failed",
                        "failure_stage": "DATABASE_CLEANUP",
                        "failure_classification": "EXTERNAL_AVAILABILITY",
                        "failure_type": type(cleanup_exc).__name__,
                        "failure_message": str(cleanup_exc),
                    }

    safe = _scrub_exact(_jsonable(report), secret)
    encoded = json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True)
    if secret and secret in encoded:
        raise RuntimeError("installer discovery trace retained PULSARA_API_KEY")
    trace = args.trace.expanduser().resolve()
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(encoded + "\n", encoding="utf-8")
    fixtures = safe.get("fixtures") if isinstance(safe, dict) else None
    compact = {
        "status": safe.get("status") if isinstance(safe, dict) else "failed",
        "passed_fixtures": (
            [
                item.get("plugin_id")
                for item in fixtures
                if isinstance(item, dict) and item.get("status") == "passed"
            ]
            if isinstance(fixtures, list)
            else []
        ),
        "failed_fixtures": (
            [
                item.get("plugin_id")
                for item in fixtures
                if isinstance(item, dict) and item.get("status") != "passed"
            ]
            if isinstance(fixtures, list)
            else []
        ),
        "failure_stage": (
            safe.get("failure_stage") if isinstance(safe, dict) else None
        ),
        "trace": os.fspath(trace),
    }
    print(json.dumps(compact, ensure_ascii=False, sort_keys=True))
    return 0 if isinstance(safe, dict) and safe.get("status") == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
