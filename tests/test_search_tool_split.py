from __future__ import annotations

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
    assert invoke(FindFilesTool(tree), glob=pattern)['files'] == expected


def test_all_content_modes_pages_and_odd_names(tree):
    tool = SearchContentTool(tree)
    first = invoke(tool, pattern='needle', limit=1)
    assert first['total_count'] == 14
    assert first['matches'] == [{'path': 'A.py', 'line': 1, 'content': 'needle needle'}]
    assert first['_hint'].endswith('offset=1.')
    second = invoke(tool, pattern='needle', limit=1, offset=1)
    assert second['matches'][0]['line'] == 2
    files = invoke(tool, pattern='needle', output_mode='files_only')
    assert files['files'] == sorted(['A.py', 'z.py', 'src/a.py', 'src/deep/a.py', 'colon:name.py', 'new\nline.py', 'other.txt'])
    counts = invoke(tool, pattern='needle', output_mode='count', limit=1, offset=2)
    assert counts['total_count'] == 14
    assert counts['counts'] == {'new\nline.py': 2}
    assert counts['truncated'] and counts['_hint'].endswith('offset=3.')
    final = invoke(tool, pattern='needle', output_mode='count', offset=6)
    assert final['counts'] == {'z.py': 2} and not final['truncated']
    assert invoke(tool, pattern='needle', output_mode='count', offset=100)['counts'] == {}
    assert 'target' not in counts


def test_single_file_filter_leading_option_and_invalid_regex(tree):
    tool = SearchContentTool(tree)
    assert invoke(tool, pattern='-needle', path='colon:name.py')['total_count'] == 1
    assert invoke(tool, pattern='needle', path='colon:name.py', file_glob='other.py')['matches'] == []
    assert invoke(tool, pattern='needle', path='src', file_glob='*.py')['total_count'] == 4
    assert invoke(FindFilesTool(tree), glob='*.py', path='colon:name.py')['files'] == ['colon:name.py']
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
    (FindFilesTool, {'glob': '*', 'path': ''}),
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
    files = invoke(FindFilesTool(tree), glob='*.py')['files']
    assert files == ['A.py', 'colon:name.py', 'new\nline.py', 'src/a.py', 'src/deep/a.py']
    tool = SearchContentTool(tree)
    for mode in ['content', 'files_only', 'count']:
        assert invoke(tool, pattern='needle', output_mode=mode)['total_count'] == (6 if mode == 'files_only' else 12)
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
    for offset in range(5):
        assert invoke(FindFilesTool(tree), glob='*.py', offset=offset, limit=1)['files']
        assert invoke(SearchContentTool(tree), pattern='needle', offset=offset, limit=1)['matches']


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
    from hashlib import sha256
    from pulsara_agent.tools.builtins.filesystem import EditFileTool
    workspace = tmp_path / 'workspace'
    external = tmp_path / 'external'
    workspace.mkdir()
    external.mkdir()
    path = external / 'sample.py'
    path.write_text('needle\n')
    found = invoke(FindFilesTool(workspace), glob='*.py', path='../external')
    assert found['workspace_relative'] is False and found['files'] == [str(path)]
    assert invoke(FindFilesTool(workspace), glob='*', path='/')['error'] == 'SEARCH_ROOT_TOO_BROAD'
    (workspace / 'sample.py').write_text('needle\n')
    assert invoke(FindFilesTool(workspace), glob='*.py')['files'] == ['sample.py']
    result = invoke(EditFileTool(workspace), path='sample.py', base_revision='sha256:' + sha256(b'needle\n').hexdigest(), operations=[{'kind':'replace_lines','start_line':1,'end_line':1,'lines':['changed']}])
    assert result['error'] == 'READ_OBSERVATION_REQUIRED'


def test_hook_review_observes_full_sealed_builtin_owner_composition(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from tests.support.round3 import prepare_test_direct_tool_surface
    async def exercise():
        port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id='review-host', session_id='review-session', live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        port.bind_subagent_port(SimpleNamespace(tool_names=('spawn_agent',)))
        port.bind_memory_port(SimpleNamespace(tool_names=('memory_get', 'remember')))
        try:
            prepare_test_direct_tool_surface(port)
            port._mcp_supervisor.inspect_discovery_catalog = lambda: SimpleNamespace(
                tools=(SimpleNamespace(semantic=SimpleNamespace(provider_tool_name="mcp__actual__read")),),
                catalog_snapshot=SimpleNamespace(servers=(SimpleNamespace(tool_surface_semantic_fingerprint=None),)))
            subjects, complete = port.observe_hook_review_tools()
            names = {subject.canonical_subject for subject in subjects}
            assert {'spawn_agent', 'memory_get', 'remember', 'find_files', 'search_content', 'mcp__actual__read'} <= names
            assert 'Agent' in next(subject.candidates for subject in subjects if subject.canonical_subject == 'spawn_agent')
            assert complete is False
        finally:
            await port.aclose()
    asyncio.run(exercise())
