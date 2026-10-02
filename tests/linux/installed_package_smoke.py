"""Run with a clean wheel-install interpreter from outside the checkout."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic

import aiohttp
import pulsara_agent
from pulsara_agent.capability.local_skill_management import (
    InstallLooseLocalSkillRequest, LocalSkillInstallDisposition,
    LocalSkillInstallScope, LocalSkillManagementService,
)
from pulsara_agent.capability.mcp_management import LocalMcpManagementService
from pulsara_agent.capability.pulsara_home import resolve_pulsara_home
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelCatalogSnapshot
from pulsara_agent.plugins.contracts import (
    InstallLocalPluginRequest, PluginInstallDisposition, PluginScopeKind,
)
from pulsara_agent.plugins.management import PluginManagementService
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from pulsara_agent.ripgrep import private_ripgrep
from pulsara_agent.settings import LocalSettingsStore
from pulsara_agent.terminal_process.environment import TerminalEnvironmentOwner, TerminalEnvConfig
from pulsara_agent.tool_permission import preset_to_policy
from pulsara_agent.tools.builtins.filesystem import FindFilesTool, SearchContentTool
from pulsara_agent.web_app.application import LocalWebApplication, packaged_static_root


class OfflineCatalogClient:
    async def fetch(self):
        return ModelCatalogSnapshot(entries={}, routes=())


async def exercise(root: Path) -> None:
    # An editable checkout or source PYTHONPATH must never satisfy this gate.
    package = Path(pulsara_agent.__file__).resolve()
    assert package.is_relative_to(Path(sys.prefix).resolve()), package
    assert 'site-packages' in package.parts, package
    launcher = Path(sys.executable).parent / 'pulsara'
    assert subprocess.check_output([str(launcher), '--version']).strip() == pulsara_agent.__version__.encode()
    rg = private_ripgrep()
    assert rg.resolve().is_relative_to(package.parent)
    assert (packaged_static_root() / 'index.html').is_file()
    (root / 'example.txt').write_text('Ubuntu package smoke\n')
    for tool, arguments, key in (
        (SearchContentTool(root), {'pattern': 'Ubuntu'}, 'matches'),
        (FindFilesTool(root), {'glob': '*.txt'}, 'files'),
    ):
        result = json.loads(tool.execute(ToolCall('wheel-smoke', tool.name, arguments)).output)
        assert result[key], result
    owner = TerminalEnvironmentOwner(root, config=TerminalEnvConfig(enable_shell_snapshot=False))
    try:
        env = owner.build(cwd=root)
        assert env.values['PATH'].split(os.pathsep)[0] == str(rg.parent)
        assert subprocess.check_output(['/bin/sh', '-c', 'rg --version'], env=env.values, cwd=root).startswith(b'ripgrep 15.2.0')
    finally:
        owner.close(timeout_seconds=1)
    home = root / 'home'
    os.environ['PULSARA_HOME'] = str(home)
    skill = root / 'portable-skill'
    skill.mkdir()
    (skill / 'SKILL.md').write_text('---\nname: portable-skill\ndescription: Portable fixture.\n---\nRun the fixture.\n')
    installed = LocalSkillManagementService().install_loose_local_skill(
        InstallLooseLocalSkillRequest(skill, LocalSkillInstallScope.USER),
    )
    assert installed.disposition is LocalSkillInstallDisposition.INSTALLED, installed
    assert (home / 'skills/portable-skill/SKILL.md').is_file()
    plugin = root / 'portable-plugin'
    plugin.mkdir()
    (plugin / 'plugin.json').write_text(json.dumps({
        '$schema': 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json',
        'name': 'portable-plugin', 'version': '1.0.0', 'description': 'Portable fixture.',
    }))
    settings = LocalSettingsStore(home / 'local-settings.yaml')
    service = PluginManagementService(
        credential_boundary=ProcessCredentialBoundary(''),
        pulsara_home_resolution=resolve_pulsara_home(str(home)),
    )
    result = await service.install_local_plugin(
        InstallLocalPluginRequest(plugin, PluginScopeKind.USER, monotonic() + 30),
        connections=LocalMcpManagementService(settings, user_config_path=home / 'mcp.yaml'),
    )
    assert result.disposition is PluginInstallDisposition.INSTALLED, result
    application = LocalWebApplication(
        settings=settings, catalog=ModelCatalogOwner(OfflineCatalogClient()),
        permission_policy=preset_to_policy("bypass-permissions"),
    )
    try:
        await application.start()
        async with aiohttp.ClientSession() as client:
            async with client.get(application.origin + '/healthz') as response:
                assert response.status == 200
                assert await response.json() == {'status': 'ready'}
            async with client.get(application.origin + '/') as response:
                assert response.status == 200
                assert '<html' in await response.text()
            async with client.get(application.origin + '/api/app/bootstrap') as response:
                assert response.status == 200
                assert (await response.json())['runtime']['status'] == 'ready'
    finally:
        await application.aclose()
    assert application._runtime_directory is None
    print('Installed wheel: launcher, assets, search, terminal rg, Skill/Plugin publication, HTTP startup/shutdown passed.')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='pulsara-wheel-smoke-') as temporary:
        asyncio.run(exercise(Path(temporary).resolve()))
