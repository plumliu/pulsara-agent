"""Real configured Chat/Responses usage calibration and cold history reads."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.provider_dispatch import ProviderDispatchCoordinator
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore, _RecordingModelRuntime, _binding, _create_database, _drop_database,
)


async def run() -> dict[str, object]:
    saved = LocalSettingsStore().read()
    scrubber = secret_scrubber(saved)
    connections = {c.target.wire_api.value: c for c in saved.model_connections
                   if c.target.model_id == 'deepseek-flash' and c.target.route_id == 'deepseek'}
    for wire in ('openai_chat_completions', 'openai_responses'):
        if wire not in connections or saved.model_api_key(connections[wire].id) is None:
            raise RuntimeError(f'missing saved configured connection/credential for {wire}')
    source_quotes = []
    original_prepare_source = ProviderDispatchCoordinator.prepare_compaction_source

    async def observe_source(owner, **kwargs):
        source = await original_prepare_source(owner, **kwargs)
        quote = source.wire_quote
        source_quotes.append({"budget_source": quote.budget_source, "budget_input_tokens": quote.budget_input_tokens,
                              "raw_final_wire_estimated_input_tokens": quote.raw_final_wire_estimated_input_tokens,
                              "anchor_model_call_id": quote.anchor_model_call_id})
        return source

    name, _, admin, runtime_dsn = _create_database(saved)
    ProviderDispatchCoordinator.prepare_compaction_source = observe_source
    original_home = os.environ.get('PULSARA_HOME')
    try:
        with TemporaryDirectory(prefix='pulsara-usage-budget-') as tmp:
            root = Path(tmp)
            home = root / 'home'
            workspace = root / 'workspace'
            home.mkdir()
            workspace.mkdir()
            os.environ['PULSARA_HOME'] = str(home)
            settings = _ReadOnlySettingsStore(replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin)))
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            delegate = ModelRuntime.production(settings=settings, catalog=catalog)
            records: list[dict[str, object]] = []
            runtime = _RecordingModelRuntime(delegate, records, None)
            core = KernelHostCore.production(model_runtime=runtime)
            results = []
            workspace_input = HostWorkspaceInput(workspace_kind='project', workspace_root=workspace, trust_workspace_mcp_config=False)
            try:
                for wire in ('openai_chat_completions', 'openai_responses'):
                    session = await core.open_session(workspace_input, system_prompt='Reply briefly and directly. Do not call tools.')
                    await session.attach_controller('provider-usage-budget-dogfood')
                    await session.update_model_call_binding(_binding(delegate, connections[wire]))
                    marker = 'USAGE_' + uuid4().hex[:10]
                    start = len(records)
                    first = await session.run_turn(PromptContent.text(f'The literal marker in this message is {marker}. Reply exactly OK.'), command_id='command:usage:'+uuid4().hex)
                    second = await session.run_turn(PromptContent.text('Reply with the exact marker from my previous message.'), command_id='command:usage:'+uuid4().hex)
                    calls = records[start:]
                    assert len(calls) == 2, calls
                    assert calls[0]['budget_source'] == 'heuristic'
                    assert calls[1]['budget_source'] == 'reported_input_anchor', calls
                    input_key = 'messages' if wire == 'openai_chat_completions' else 'input'
                    before, after = calls[0]['provider_input'], calls[1]['provider_input']
                    assert after[input_key][:len(before[input_key])] == before[input_key]
                    assert {k: v for k, v in after.items() if k != input_key} == {k: v for k, v in before.items() if k != input_key}
                    assert calls[1]['anchor_model_call_id'] == calls[0]['resolved_model_call_id']
                    assert marker in second.final_text, second.final_text
                    page = await core.read_provider_call_usage_page(session_id=session.session_id)
                    assert len(page.usages) == 2, page.to_dict()
                    assert all(row['input_tokens'] is not None and row['input_tokens'] > 0 for row in page.usages)
                    source_start = len(source_quotes)
                    compaction = await session.compact_context(command_id='command:usage-compact:'+uuid4().hex, force=True)
                    scoped_source_quotes = source_quotes[source_start:]
                    assert scoped_source_quotes and scoped_source_quotes[0]['budget_source'] == 'reported_input_anchor', scoped_source_quotes
                    assert len((await core.read_provider_call_usage_page(session_id=session.session_id)).usages) == 2  # summary purpose excluded
                    session_id = session.session_id
                    await core.close_session(session.host_session_id)
                    cold = await core.read_provider_call_usage_page(session_id=session_id)
                    assert cold.to_dict() == page.to_dict()
                    resumed = await core.resume_session(session_id, workspace_input=workspace_input, system_prompt='Reply briefly and directly. Do not call tools.')
                    assert all(slot.usage_anchor is None for slot in resumed._input_continuity._slots.values())
                    await resumed.attach_controller('provider-usage-budget-dogfood-resume')
                    after_restart = await resumed.run_turn(PromptContent.text('Reply RESTART_OK.'), command_id='command:usage:'+uuid4().hex)
                    assert records[-1]['budget_source'] == 'heuristic'
                    final_history = await core.read_provider_call_usage_page(session_id=session_id)
                    assert len(final_history.usages) == 3
                    results.append({'wire_api': wire, 'session_id': session_id, 'first': first.final_text,
                                    'second': second.final_text, 'after_restart': after_restart.final_text,
                                    'calls': records[start:], 'history': final_history.to_dict(), 'compaction': str(compaction), 'compaction_source_quotes': scoped_source_quotes})
                    await core.close_session(resumed.host_session_id)
            finally:
                await core.shutdown()
            evidence = {'completed_at': datetime.now(timezone.utc).isoformat(), 'results': results}
            # Scrub actual saved credential values, preserving concrete prompts/replies/counts.
            return json.loads(scrubber.scrub_text(json.dumps(evidence, ensure_ascii=False)))
    finally:
        if original_home is None:
            os.environ.pop('PULSARA_HOME', None)
        else:
            os.environ['PULSARA_HOME'] = original_home
        _drop_database(saved, name)
        ProviderDispatchCoordinator.prepare_compaction_source = original_prepare_source


def main() -> None:
    evidence = asyncio.run(run())
    destination = Path('output/provider-usage-budget-20261004/dogfood.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'evidence': str(destination.resolve()), 'wires': [result['wire_api'] for result in evidence['results']]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
