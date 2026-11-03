from __future__ import annotations
from tests.support.model_config import scheduled_task_service

import json
import os
import subprocess

import pytest
from jsonschema import Draft202012Validator

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.hooks.matcher import tool_matcher_subject
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.ripgrep import RipgrepUnavailable, private_ripgrep
from pulsara_agent.terminal_process.environment import TerminalEnvironmentOwner, TerminalEnvConfig
from pulsara_agent.tools.builtins.filesystem import FindFilesTool, SearchContentTool


def invoke(tool, **arguments):
    return json.loads(tool.execute(ToolCall('search-test', tool.name, arguments)).output)


@pytest.fixture
def tree(tmp_path):
    for name in ['A.py', 'z.py', 'src/a.py', 'src/deep/a.py', 'colon:name.py', 'new\nline.py', 'other.txt']:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('needle needle\n-needle\nabsent\n')
    return tmp_path


@pytest.mark.parametrize(('pattern', 'expected'), [
    ('a.py', ['src/a.py', 'src/deep/a.py']),
    ('src/*.py', ['src/a.py']),
    ('src/**/*.py', ['src/a.py', 'src/deep/a.py']),
    ('**/*.py', ['A.py', 'colon:name.py', 'new\nline.py', 'src/a.py', 'src/deep/a.py', 'z.py']),
    ('*name*', ['colon:name.py']),
    ('name', []),
])
def test_relative_glob_and_exact_basename(tree, pattern, expected):
    assert [item["path"] for item in invoke(FindFilesTool(tree), glob=pattern)["targets"]] == expected


def test_all_content_modes_pages_and_odd_names(tree):
    tool = SearchContentTool(tree)
    first = invoke(tool, pattern='needle', limit=1)
    assert 'total_count' not in first and first['status'] == 'ok'
    assert first['matches'][0]['target'] == {"kind": "PATH", "path": 'A.py'}
    assert first['matches'][0]['line'] == 1 and first['matches'][0]['excerpt'] == 'needle needle'
    assert first['matches'][0]['excerpt_partial'] is False
    assert first['next']['tool'] == tool.name
    second = invoke(tool, **first['next']['arguments'])
    assert second['matches'][0]['line'] == 2
    files = invoke(tool, pattern='needle', output_mode='files_only')
    assert [item["path"] for item in files["targets"]] == sorted(['A.py', 'z.py', 'src/a.py', 'src/deep/a.py', 'colon:name.py', 'new\nline.py', 'other.txt'])
    counts = invoke(tool, pattern='needle', output_mode='count', limit=1)
    counts = invoke(tool, **counts['next']['arguments'])
    counts = invoke(tool, **counts['next']['arguments'])
    assert counts['counts'] == [{'target': {'kind': 'PATH', 'path': 'new\nline.py'}, 'count': 2}]
    collected = list(counts['counts'])
    while counts['next'] is not None:
        counts = invoke(tool, **counts['next']['arguments'])
        collected.extend(counts['counts'])
    assert {item['target']['path'] for item in collected} == {'new\nline.py', 'other.txt', 'src/a.py', 'src/deep/a.py', 'z.py'}
    assert sum(item['count'] for item in collected) == 10
    assert counts['next'] is None and 'total_count' not in counts


def test_single_file_filter_leading_option_and_invalid_regex(tree):
    tool = SearchContentTool(tree)
    assert len(invoke(tool, pattern='-needle', target={"kind":"PATH","path":'colon:name.py'})['matches']) == 1
    assert invoke(tool, pattern='needle', target={"kind":"PATH","path":'colon:name.py'}, file_glob='other.py')['matches'] == []
    assert len(invoke(tool, pattern='needle', target={"kind":"PATH","path":'src'}, file_glob='*.py')['matches']) == 4
    assert [item["path"] for item in invoke(FindFilesTool(tree), glob='*.py', target={"kind":"PATH","path":'colon:name.py'})["targets"]] == ['colon:name.py']
    with pytest.raises(RuntimeError, match='regex'):
        invoke(tool, pattern='(', file_glob='no-match')


@pytest.mark.parametrize('tool_type,arguments', [
    (SearchContentTool, {'pattern': ''}), (FindFilesTool, {'glob': ''}),
    (SearchContentTool, {'pattern': 1}), (FindFilesTool, {'glob': '*.py', 'pattern': 'x'}),
    (SearchContentTool, {'pattern': 'x', 'target': 'content'}),
    (SearchContentTool, {'pattern': 'x', 'glob': '*.py'}),
    (SearchContentTool, {'pattern': 'x', 'file_glob': ''}),
    (SearchContentTool, {'pattern': 'x', 'offset': -1}),
    (SearchContentTool, {'pattern': 'x', 'limit': True}),
    (SearchContentTool, {'pattern': 'x', 'limit': '50'}),
    (SearchContentTool, {'pattern': 'x', 'limit': 1001}),
    (FindFilesTool, {'glob': '*', 'output_mode': 'count'}),
    (FindFilesTool, {'glob': '*', "target": {"kind": "PATH", "path": ''}}),
])
def test_closed_schema_and_runtime(tree, tool_type, arguments):
    tool = tool_type(tree)
    schema = builtin_tool_catalog_entry(tool.name).descriptor.input_schema
    assert list(Draft202012Validator(schema).iter_errors(arguments))
    with pytest.raises(ValueError):
        invoke(tool, **arguments)


def test_private_backend_config_ignore_hidden_binary_symlink(tree, monkeypatch):
    fake = tree / 'bin'
    fake.mkdir()
    (fake / 'rg').write_text('#!/bin/sh\nexit 99\n')
    (fake / 'rg').chmod(0o755)
    monkeypatch.setenv('PATH', str(fake))
    config = tree / 'rgconfig'
    config.write_text('--hidden\n--files\n')
    monkeypatch.setenv('RIPGREP_CONFIG_PATH', str(config))
    (tree / '.ignore').write_text('z.py\nrgconfig\nbin/\n')
    (tree / '.hidden.py').write_text('needle')
    (tree / 'binary.bin').write_bytes(b'\0needle\n')
    (tree / 'link.py').symlink_to(tree / 'A.py')
    files = [item["path"] for item in invoke(FindFilesTool(tree), glob='*.py')["targets"]]
    assert files == ['A.py', 'colon:name.py', 'new\nline.py', 'src/a.py', 'src/deep/a.py']
    tool = SearchContentTool(tree)
    for mode in ['content', 'files_only', 'count']:
        result = invoke(tool, pattern='needle', output_mode=mode)
        assert (len(result['targets']) if mode == 'files_only' else sum(item['count'] for item in result['counts']) if mode == 'count' else len(result['matches'])) == (6 if mode == 'files_only' else 12)
    with pytest.raises(RuntimeError):
        invoke(tool, pattern='(?=needle)')


@pytest.mark.parametrize('snapshot', [False, True])
def test_terminal_private_path_venv_child_and_dependency_failure(tree, monkeypatch, snapshot):
    venv = tree / '.venv/bin'
    venv.mkdir(parents=True)
    (venv / 'rg').write_text('#!/bin/sh\necho fake\n')
    (venv / 'rg').chmod(0o755)
    owner = TerminalEnvironmentOwner(tree, config=TerminalEnvConfig(enable_shell_snapshot=snapshot), parent_env={'PATH': '/usr/bin:/bin', 'SHELL': '/bin/sh', 'HOME': str(tree)})
    try:
        environment = owner.build(cwd=tree)
        assert environment.values['PATH'].split(os.pathsep)[0] == str(private_ripgrep().parent)
        assert subprocess.check_output(['/bin/sh', '-c', 'cd src; /bin/sh -c "rg --version"'], env=environment.values, cwd=tree).startswith(b'ripgrep 15.2.0')
        monkeypatch.setattr('pulsara_agent.terminal_process.environment.private_ripgrep', lambda: (_ for _ in ()).throw(RipgrepUnavailable('test unavailable')))
        fallback = owner.build(cwd=tree)
        assert fallback.diagnostic['ripgrep_error'] == 'test unavailable'
        assert fallback.values['PATH'].split(os.pathsep)[0] == str(venv)
        assert subprocess.check_output(['/bin/sh', '-c', 'printf working'], env=fallback.values) == b'working'
    finally:
        owner.close(timeout_seconds=2)


def test_closed_aliases_and_repeat_state_pages(tree):
    assert tool_matcher_subject('search_content').candidates == ('search_content', 'Grep')
    assert tool_matcher_subject('find_files').candidates == ('find_files', 'Glob')
    assert tool_matcher_subject('read_file').candidates == ('read_file', 'Read')
    for tool, arguments, key in [(FindFilesTool(tree), {'glob': '*.py', 'limit': 1}, 'targets'),
                                  (SearchContentTool(tree), {'pattern': 'needle', 'limit': 1}, 'matches')]:
        pages = []
        while True:
            page = invoke(tool, **arguments)
            pages.extend(page[key])
            if page['next'] is None:
                break
            arguments = page['next']['arguments']
        assert len(pages) == (6 if key == 'targets' else 14)


@pytest.mark.parametrize('condition', ['missing', 'nonexecutable', 'wrong-version', 'not-an-executable'])
def test_private_dependency_visible_error_without_path_fallback(tmp_path, monkeypatch, condition):
    from pulsara_agent import ripgrep
    root = tmp_path / 'package with spaces'
    executable = root / '_vendor/ripgrep/rg'
    executable.parent.mkdir(parents=True)
    monkeypatch.setattr(ripgrep, '__file__', str(root / 'ripgrep.py'))
    if condition != 'missing':
        executable.write_bytes(b'not an executable' if condition == 'not-an-executable' else b'#!/bin/sh\nprintf "ripgrep 1.0\\n"\n')
        executable.chmod(0o644 if condition == 'nonexecutable' else 0o755)
    with pytest.raises(RipgrepUnavailable, match='内置搜索依赖不可用'):
        private_ripgrep()


def test_find_files_external_scope_and_search_observations_do_not_authorize_edit(tmp_path):
    from pulsara_agent.identity import content_revision
    from pulsara_agent.tools.builtins.filesystem import EditFileTool
    workspace = tmp_path / 'workspace'
    external = tmp_path / 'external'
    workspace.mkdir()
    external.mkdir()
    path = external / 'sample.py'
    path.write_text('needle\n')
    found = invoke(FindFilesTool(workspace), glob='*.py', target={"kind":"PATH","path":'../external'})
    assert found['workspace_relative'] is False and [item["path"] for item in found["targets"]] == [str(path)]
    assert invoke(FindFilesTool(workspace), glob='*', target={"kind":"PATH","path":'/'})['error'] == 'SEARCH_ROOT_TOO_BROAD'
    (workspace / 'sample.py').write_text('needle\n')
    assert [item["path"] for item in invoke(FindFilesTool(workspace), glob='*.py')["targets"]] == ['sample.py']
    result = invoke(EditFileTool(workspace), target={"kind":"PATH","path":'sample.py'}, base_revision=content_revision(b'needle\n'), operations=[{'kind':'replace_lines','start_line':1,'end_line':1,'lines':['changed']}])
    assert result['error'] == 'READ_OBSERVATION_REQUIRED'


def test_hook_review_observes_full_sealed_builtin_owner_composition(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from tests.support.round3 import prepare_test_direct_tool_surface
    async def exercise():
        port = DirectKernelToolPort(scheduled_tasks=scheduled_task_service(), workspace_root=tmp_path, host_owner_id='review-host', session_id='session:zazc4gzsyde7brlnkyse2hjvpm', live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        port.bind_subagent_port(SimpleNamespace(tool_names=('run_agent',)))
        port.bind_memory_port(SimpleNamespace(tool_names=('get_memory', 'remember')))
        try:
            prepare_test_direct_tool_surface(port)
            port._mcp_supervisor.inspect_discovery_catalog = lambda: SimpleNamespace(
                tools=(SimpleNamespace(semantic=SimpleNamespace(provider_tool_name="mcp__actual__read")),),
                catalog_snapshot=SimpleNamespace(servers=(SimpleNamespace(tool_surface_semantic_fingerprint=None),)))
            subjects, complete = port.observe_hook_review_tools()
            names = {subject.canonical_subject for subject in subjects}
            assert {'run_agent', 'get_memory', 'remember', 'find_files', 'search_content', 'mcp__actual__read'} <= names
            assert 'Agent' in next(subject.candidates for subject in subjects if subject.canonical_subject == 'run_agent')
            assert complete is False
        finally:
            await port.aclose()
    asyncio.run(exercise())
