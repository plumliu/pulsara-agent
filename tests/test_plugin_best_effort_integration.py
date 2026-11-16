"""Imported Hook declarations remain actionable through capability owners."""

from __future__ import annotations

import asyncio
import json
from time import monotonic

import pytest

from pulsara_agent.capability.management_form import AcceptedCapabilityFormSubmission
from pulsara_agent.conversation_kernel.capability_management_execution import CapabilityManagementCall
from pulsara_agent.conversation_kernel.capability_query import CapabilitySourceQuery
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.plugins.contracts import (
    GcLocalPluginPackagesRequest,
    InstallLocalPluginRequest,
    PluginScopeKind,
    RemoveLocalPluginRequest,
)
from tests.test_capability_management_preparation import preparation


async def _remove_and_collect(service, installed):
    removed = await service.plugins.remove_local_plugin(RemoveLocalPluginRequest(
        installed.identity.scope, installed.identity.plugin_id,
        monotonic() + 30, installed.package_install_id,
    ))
    assert removed.disposition.value == 'REMOVED'
    collected = service.plugins.gc_local_plugin_packages(GcLocalPluginPackagesRequest(monotonic() + 30))
    assert collected.disposition.value == 'COMPLETE'
    assert not installed.package_root.exists()
    await service.mcp.aclose()


@pytest.mark.parametrize('overlay', [False, True])
@pytest.mark.parametrize('declaration_link', [False, True])
def test_native_openai_hook_source_can_be_queried_and_reviewed(tmp_path, overlay, declaration_link):
    async def run():
        service = preparation(tmp_path)
        source = tmp_path / 'openai-source'
        source.mkdir()
        manifest = {
            '$schema': 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json',
            'name': 'openai-example',
        }
        settings = {'hooks': ['./hooks/first.json', './hooks/second.json']}
        if overlay:
            declaration = '.codex-plugin/plugin.json'
            selected = source / declaration
            selected.parent.mkdir()
            selected.write_text(json.dumps({'name': 'foreign-display-name', **settings}))
        else:
            declaration = 'plugin.json'
            manifest['extensions'] = {'com.openai': settings}
        (source / 'plugin.json').write_text(json.dumps(manifest))
        if declaration_link:
            selected = source / declaration
            physical = source / 'metadata' / selected.name
            physical.parent.mkdir()
            selected.rename(physical)
            selected.symlink_to('../metadata/plugin.json' if overlay else 'metadata/plugin.json')
        (source / 'hooks').mkdir()
        for filename, text in [('first.json', 'first'), ('second.json', 'second')]:
            (source / 'hooks' / filename).write_text(json.dumps({
                'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': f'echo {text}'}]}]},
            }))
        installed = await service.plugins.install_local_plugin(
            InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 30),
            connections=service.mcp,
        )
        assert installed.disposition.value == 'INSTALLED', installed
        assert installed.identity.plugin_id == 'openai-example'
        assert not installed.enabled
        target = {'kind': 'HOOK_SOURCE', 'config_path': str((installed.package_root / declaration).resolve())}
        parent = {'kind': 'PLUGIN', 'plugin_path': str(installed.package_root.parent)}

        async def query(arguments):
            return await CapabilitySourceQuery(
                service, runtime=None, scope=ModelInputScopeKind.ROOT,
                inspect_tool=None, mcp_configs=(),
            ).execute(arguments)

        detail = await query({'kind': 'PLUGIN', 'target': parent})
        assert target in [part['value'] for part in detail['content'] if part['type'] == 'target']
        hooks = await query({'kind': 'HOOK', 'target': target})
        assert hooks['meta']['status'] == 'SUCCESS'
        record, = [part['value'] for part in hooks['content'] if part['type'] == 'object']
        assert record['target'] == target
        assert [item['command'] for item in record['definitions']] == ['echo first', 'echo second']
        listed = await query({'kind': 'HOOK', 'parent': parent})
        assert [part['value']['target'] for part in listed['content'] if part['type'] == 'object'] == [target]

        call = CapabilityManagementCall(service, await service.prepare({
            'action': 'TRUST_HOOK_SOURCE', 'target': target,
        }))
        assert call.prepared.hook.provenance.identity.canonical_path == (installed.package_root / declaration).resolve()
        form = call.form('review-openai-hooks')
        call.accept(AcceptedCapabilityFormSubmission(await form.prepare_submission({'hook_review_accepted': True})))
        result = await call.execute()
        assert result.state == 'SUCCESS'
        trust = service.hooks.trust_store.assess(
            call.prepared.hook.provenance.trust_subject,
            call.prepared.hook.trust.current_definition_digest,
        )
        assert trust.disposition.value == 'TRUSTED'
        state = service.plugins._store().read_state(service.plugins._store().layout(installed.identity))
        assert not state.enabled
        with pytest.raises(ValueError, match='declared configuration'):
            await service.prepare({
                'action': 'TRUST_HOOK_SOURCE',
                'target': {'kind': 'HOOK_SOURCE', 'config_path': str(installed.package_root / 'hooks/first.json')},
            })
        await _remove_and_collect(service, installed)

    asyncio.run(run())


def test_native_mcp_config_link_returns_a_manageable_connection_target(tmp_path):
    async def run():
        service = preparation(tmp_path)
        source = tmp_path / 'mcp-source'
        (source / 'configuration').mkdir(parents=True)
        (source / 'plugin.json').write_text(json.dumps({
            '$schema': 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json',
            'name': 'linked-mcp',
        }))
        (source / 'configuration/servers.json').write_text(json.dumps({
            '$schema': 'https://agent-plugins.org/schemas/1.0.0/mcp.schema.json',
            'mcpServers': {'docs': {'type': 'streamable-http', 'url': 'https://example.org/mcp'}},
        }))
        (source / 'mcp.json').symlink_to('configuration/servers.json')
        installed = await service.plugins.install_local_plugin(
            InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 30),
            connections=service.mcp,
        )
        assert installed.disposition.value == 'INSTALLED', installed
        target = {'kind': 'MCP_CONNECTION', 'server_id': 'docs',
                  'config_path': str((installed.package_root / 'mcp.json').resolve())}
        parent = {'kind': 'PLUGIN', 'plugin_path': str(installed.package_root.parent)}
        result = await CapabilitySourceQuery(
            service, runtime=None, scope=ModelInputScopeKind.ROOT,
            inspect_tool=None, mcp_configs=(),
        ).execute({'kind': 'MCP', 'parent': parent})
        assert result['meta']['status'] == 'SUCCESS'
        record, = [part['value'] for part in result['content'] if part['type'] == 'object']
        assert record['target'] == target
        prepared = await service.prepare({
            'action': 'UPDATE_MCP_CONNECTION', 'target': target,
            'config': {'public_headers': {'X-Tenant': 'public'}},
        })
        assert prepared.plugin.identity == installed.identity
        await _remove_and_collect(service, installed)

    asyncio.run(run())


def test_reading_a_linked_plugin_skill_preserves_its_required_compaction_reread(tmp_path):
    from pulsara_agent.conversation_kernel.compaction.retained_skill import freeze_retained_skill_context
    from pulsara_agent.identity import random_id
    from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV3
    from pulsara_agent.ports.tool_execution import ToolCall
    from pulsara_agent.primitives.context import thaw_json
    from pulsara_agent.tools.builtins.filesystem import ReadFileTool
    from tests.support.plugin_review import enable_reviewed_plugin
    from tests.test_round5b_long_horizon_context_compaction import _retained_read

    async def run():
        service = preparation(tmp_path)
        source = tmp_path / 'linked-skill-source'
        document = source / 'skill-resources' / 'guide' / 'SKILL.md'
        document.parent.mkdir(parents=True)
        document.write_text('---\nname: guide\ndescription: Use this project guide.\n---\nRead the guide before working.\n')
        (source / 'skills').symlink_to('skill-resources', target_is_directory=True)
        (source / 'plugin.json').write_text(json.dumps({
            '$schema': 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json',
            'name': 'linked-skill',
        }))
        installed = await service.plugins.install_local_plugin(
            InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 30),
            connections=service.mcp,
        )
        assert installed.disposition.value == 'INSTALLED', installed
        try:
            enabled = enable_reviewed_plugin(service.plugins, installed, monotonic() + 30)
            assert enabled.disposition.value == 'ENABLED', enabled
            definitions = service.inspect_plugin_skill_definitions(service.workspace_root)
            _loose, _bundled, inspection = service.skills.inspect_definition_sources(
                workspace_root=service.workspace_root, plugin_definitions=definitions,
                deadline_monotonic=monotonic() + 30,
            )
            skill, = definitions.candidates
            read = ReadFileTool(service.workspace_root).execute(ToolCall(
                random_id('call'), 'read_file',
                {'target': {'kind': 'PATH', 'path': skill.skill_path}, 'limit': 1},
            ))
            assert read.status.value == 'success'
            read_path = thaw_json(read.business_value.public_fields)['target']['path']
            physical_path = str(installed.package_root / 'skill-resources' / 'guide' / 'SKILL.md')
            assert read_path == physical_path
            selection = freeze_retained_skill_context(
                canonical_read=_retained_read((read_path,)), predecessor_epoch=None,
                inspection=inspection, estimator=PulsaraHeuristicTokenEstimatorV3(),
            )
            assert [(item.name, item.skill_path) for item in selection.ordered_items] == [('guide', physical_path)]
            assert 'read_file' in selection.rendered_body
            assert skill.body not in selection.rendered_body
        finally:
            await _remove_and_collect(service, installed)

    asyncio.run(run())
