from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.web_app.path_completion import complete_workspace_paths
from pulsara_agent.web_app.session_controller import LocalSessionController


def test_paths_only_progressive_prefixes_and_no_contents(tmp_path, monkeypatch):
    nested = tmp_path / '文档 with space'
    nested.mkdir()
    (nested / 'Report.PDF').write_bytes(b'not parsed')
    (tmp_path / '.hidden').touch()
    (tmp_path / 'bad\nname').touch()
    (tmp_path / 'alias').symlink_to(nested, target_is_directory=True)
    monkeypatch.setattr(Path, 'open', lambda *a, **kw: pytest.fail('must not read file contents'))
    page = complete_workspace_paths(tmp_path, '', None)
    assert [item['name'] for item in page['items']] == ['.hidden', 'alias', '文档 with space']
    assert page['items'][1]['kind'] == 'directory'
    page = complete_workspace_paths(tmp_path, '文档 with space/re', None)
    assert page == {
        'directory': str(nested),
        'items': [{'name': 'Report.PDF', 'path': str(nested / 'Report.PDF'),
                   'relative_path': '文档 with space/Report.PDF', 'kind': 'file'}],
        'next_cursor': None,
    }
    assert complete_workspace_paths(nested, '../.h', None)['items'][0]['name'] == '.hidden'
    assert complete_workspace_paths(tmp_path, 'absent-prefix', None)['items'] == []
    with pytest.raises(FileNotFoundError):
        complete_workspace_paths(tmp_path, 'missing/', None)
    for prefix in ['/absolute', '\x00', 'bad\n']:
        with pytest.raises(ValueError):
            complete_workspace_paths(tmp_path, prefix, None)


def test_paging_has_no_total_candidate_cap(tmp_path):
    # The spec bounds one HTTP/render batch to 100, not the accessible directory.
    names = [f'file-{i:03}.txt' for i in range(237)]
    for name in reversed(names):
        (tmp_path / name).touch()
    cursor = None
    found = []
    sizes = []
    while True:
        page = complete_workspace_paths(tmp_path, 'file-', cursor)
        found.extend(item['name'] for item in page['items'])
        sizes.append(len(page['items']))
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert found == names
    assert sizes == [100, 100, 37]


def test_session_root_comes_from_canonical_selected_session(tmp_path):
    async def exercise():
        first, second = tmp_path / 'one', tmp_path / 'two'
        first.mkdir()
        second.mkdir()
        (first / 'first.txt').touch()
        (second / 'second.txt').touch()
        controller = object.__new__(LocalSessionController)
        controller.workspace_input = SimpleNamespace(memory_domain_id='domain', root=first)
        controller.core = SimpleNamespace(read_resumable_session=AsyncMock(side_effect=[
            SimpleNamespace(workspace_root=str(second)), None,
        ]))
        result = await controller.complete_workspace_paths('session-two', '', None)
        assert [item['name'] for item in result['items']] == ['second.txt']
        controller.core.read_resumable_session.assert_awaited_with('session-two', memory_domain_id='domain')
        with pytest.raises(KeyError):
            await controller.complete_workspace_paths('missing', '', None)
    asyncio.run(exercise())
