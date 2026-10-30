"""Real saved Firecrawl + model, enabled after the first turn with TUN unchanged.

Production settings/configuration are read-only. The explicit test-input hard cut
removes the retired network field from a temporary copy, never from the source.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import yaml

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.capability.mcp_management import config_guard
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from pulsara_agent.desktop_runtime import validate_managed_postgres
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import model_call_binding_from_dict
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner
from pulsara_agent.tool_permission import preset_to_policy
from tests.dogfood.run_model_switch_handover_dogfood import _ReadOnlySettingsStore, _RecordingModelRuntime


def desktop_postgres(home):
    def dsn(role, database="pulsara"):
        return make_conninfo(host=str(home / "desktop/socket"), port=5432, dbname=database, user=role)
    postgres = LocalPostgresConfig(dsn("pulsara_runtime"), dsn("pulsara_admin"))
    validate_managed_postgres(postgres)
    return postgres, dsn


async def run():
    home = require_pulsara_home()
    saved_bytes = {name: (home / name).read_bytes() for name in ("local-settings.yaml", "mcp.yaml")}
    postgres, dsn = desktop_postgres(home)
    saved = LocalSettingsStore(home / "local-settings.yaml", postgres=postgres).read()
    scrub = saved.credential_scrub_set()
    source = yaml.safe_load((home / "mcp.yaml").read_text())["servers"]["firecrawl"]
    removed = source["transport"].pop("network_policy", None)
    source["enabled"] = False
    with psycopg.connect(postgres.admin_dsn, row_factory=dict_row) as c:
        c.read_only = True
        assert c.execute("select current_database() name,inet_server_addr() address").fetchone() == {"name": "pulsara", "address": None}
        current = c.execute("select id,title,model_call_binding from pulsara_v3.sessions where root_session_id is null order by updated_at desc limit 1").fetchone()
        binding = model_call_binding_from_dict(current["model_call_binding"])
    assert binding is not None
    connection = next(c for c in saved.model_connections if c.id == binding.connection_id)
    assert saved.model_api_key(connection.id)
    network = {}
    verge = Path.home() / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml"
    if verge.exists():
        config = yaml.safe_load(verge.read_text())
        network = {"tun_enabled": config.get("tun", {}).get("enable"),
                   "dns_mode": config.get("dns", {}).get("enhanced-mode"),
                   "mixed_port": config.get("mixed-port")}
        assert network["tun_enabled"] is True, "This acceptance run requires the existing Clash TUN to remain enabled"
    network["system_dns_addresses"] = sorted({row[4][0] for row in socket.getaddrinfo("mcp.firecrawl.dev", 443, type=socket.SOCK_STREAM)})
    evidence = {"started_at": datetime.now(timezone.utc).isoformat(), "network": network,
                "source_session": {"id": current["id"], "title": current["title"]},
                "model": connection.target.model_id, "removed_test_input_field": removed,
                "calls": [], "turns": [], "checks": {}}
    output = Path("output/diagnostics/mcp-host-network/real-provider.json").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    database = "pulsara_mcp_network_" + uuid4().hex[:12]
    admin, runtime_dsn = dsn("pulsara_admin", database), dsn("pulsara_runtime", database)
    previous_home = os.environ.get("PULSARA_HOME")
    created = False
    try:
        with psycopg.connect(postgres.admin_dsn, autocommit=True) as c:
            c.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
        created = True
        report = PostgresMigrationRunner(admin_dsn=admin, runtime_dsn=runtime_dsn).migrate(deadline_monotonic=monotonic() + 240)
        assert report.migration_head_version == 0
        with TemporaryDirectory(prefix="pulsara-mcp-network-") as temporary:
            base = Path(temporary).resolve()
            test_home, workspace = base / "home", base / "workspace"
            test_home.mkdir()
            workspace.mkdir()
            (test_home / "mcp.yaml").write_text(yaml.safe_dump({"servers": {"firecrawl": source}}))
            os.environ["PULSARA_HOME"] = str(test_home)
            injected = _ReadOnlySettingsStore(replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin)))
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            delegate = ModelRuntime.production(settings=injected, catalog=catalog)
            runtime = _RecordingModelRuntime(delegate, evidence["calls"])
            core = KernelHostCore.production(model_runtime=runtime)
            controller = LocalSessionController(core=core, permission_policy=preset_to_policy("bypass-permissions"), active_skill_names=frozenset())
            try:
                handle = await controller.create_session(workspace_kind="project", workspace_path=str(workspace), model_call_binding=binding)
                host = handle.session
                inputs = []
                previous = host._runner._provider_input_installed_observer
                def observe(request):
                    compiled = request.compiled_input
                    if inputs:
                        old = inputs[-1]
                        assert compiled.system_prompt == old.system_prompt
                        assert compiled.tools == old.tools
                        assert compiled.messages[:len(old.messages)] == old.messages
                    inputs.append(compiled)
                    if previous:
                        previous(request)
                host._runner._provider_input_installed_observer = observe
                async def turn(label, text):
                    print("Running", label, flush=True)
                    result = await host.run_turn(PromptContent.text(text))
                    evidence["turns"].append({"label": label, "prompt": text, "reply": result.final_text})
                    print(scrub.scrub_text("Reply: " + result.final_text), flush=True)
                    return result
                await turn("disabled_first_turn", "你好，请只回复：你好。不要调用工具。")
                assert len(inputs) >= 1
                assert not any("firecrawl" in spec.name for spec in inputs[0].tools)
                disabled = next(c for c in core.mcp_management.load_configs() if c.server_id == "firecrawl")
                enabled = {**source, "enabled": True}
                outcome = await controller.update_user_mcp_server(server_id="firecrawl", config=enabled, expected=config_guard(disabled))
                assert outcome["operation"]["success"]
                evidence["enable_outcome"] = outcome["adoption"]
                await turn("late_enabled_meta_call", "我刚在能力页启用了 Firecrawl。请先查询它的工具目录并检查输入结构，再通过 meta 工具 use_new_mcp_tool 做一次最小只读验证：抓取 https://example.com/，告诉我页面标题和一句简介。若目录仍在发现中，可以等待后再查。不要用终端、脚本、REST 或其他工具绕过 MCP。")
                inspection = host.inspect_capability_catalog()
                servers = inspection.mcp_catalog.servers
                firecrawl = next(s for s in servers if s.server_id == "firecrawl")
                evidence["catalog"] = {"status": firecrawl.status.value, "tools": firecrawl.exposed_tool_count,
                    "resources": firecrawl.resource_count, "templates": firecrawl.resource_template_count}
                assert firecrawl.status.value == "READY"
                assert firecrawl.exposed_tool_count > 0
                with psycopg.connect(admin, row_factory=dict_row) as c:
                    c.read_only = True
                    rows = c.execute("""select coalesce(b.tool_name,i.tool_name) tool_name,
                        coalesce(b.tool_arguments,i.original_arguments) arguments,
                        r.result_state, r.observation_origin_kind,
                        convert_from(coalesce(e.inline_content,bl.body),'UTF8') result
                        from pulsara_v3.tool_invocations i
                        left join pulsara_v3.assistant_message_blocks b on b.assistant_entry_id=i.assistant_entry_id and b.tool_call_id=i.tool_call_id
                        left join pulsara_v3.tool_results r on r.invocation_id=i.id
                        left join pulsara_v3.transcript_entries e on e.id=r.result_entry_id
                        left join pulsara_v3.blobs bl on bl.id=e.blob_id
                        where i.session_id=%s order by r.observed_at""", (host.session_id,)).fetchall()
                evidence["tool_results"] = rows
                meta = [r for r in rows if r["tool_name"] == "use_new_mcp_tool"]
                assert len(meta) == 1, f"Expected one real meta call, found {len(meta)}"
                assert meta[0]["result_state"] == "SUCCESS", meta[0]
                assert "Example Domain" in meta[0]["result"]
                assert not any(r["tool_name"] in {"exec_process", "read_file", "write_file"} for r in rows)
                assert any(r["tool_name"] == "query_capabilities" and '"META"' in r["result"] for r in rows)
                assert len(inputs) > 1
                assert all((home / name).read_bytes() == body for name, body in saved_bytes.items())
                evidence["checks"] = {"same_epoch_prefix_continuity": True, "late_enabled_meta_route": True,
                    "one_real_meta_call_succeeded": True, "no_terminal_or_rest_bypass": True,
                    "production_settings_unchanged": True, "clean_v0": True}
                evidence["provider_input_count"] = len(inputs)
            finally:
                await controller.aclose()
                await core.shutdown()
    finally:
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        if created:
            with psycopg.connect(postgres.admin_dsn, autocommit=True) as c:
                c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
        output.write_text(scrub.scrub_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str)))
        print("Evidence:", output, flush=True)
    return evidence


if __name__ == "__main__":
    asyncio.run(run())
