"""Outside-checkout, isolated-interpreter checks against the final Mac package."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pulsara_agent
from pulsara_agent.desktop_app.application import packaged_static_root
from pulsara_agent.ripgrep import private_ripgrep
from pulsara_agent.terminal_process.environment import TerminalEnvironmentOwner, TerminalEnvConfig
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.tools.builtins.filesystem import FindFilesTool, SearchContentTool

package = Path(pulsara_agent.__file__).resolve()
assert package.is_relative_to(Path(sys.prefix).resolve())
assert 'site-packages' in package.parts
assert (packaged_static_root() / 'index.html').is_file()
with tempfile.TemporaryDirectory(prefix="Mac 中文 CLI's-") as temporary:
    root = Path(temporary).resolve()
    linked_python = root / "Python 中文's"
    linked_python.symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)
    launcher = root / 'pulsara'
    repository = Path(__file__).resolve().parents[3]
    node = "const f=require('node:fs');const {cliLauncherScript}=require(process.argv[1]);f.writeFileSync(process.argv[2],cliLauncherScript(process.argv[3]),{mode:0o755,flag:'wx'});"
    subprocess.run(['node', '-e', node, str(repository / 'desktop/dist/cli-install.js'), str(launcher), str(linked_python / 'bin/python3')], check=True)
    version = subprocess.check_output([str(launcher), '--version'], cwd=root, text=True).strip()
    assert version == pulsara_agent.__version__
    help_text = subprocess.check_output([str(launcher), '--help'], cwd=root, text=True)
    assert help_text.startswith('usage: pulsara ') and '--version' in help_text
    (root / "文件 中文's.txt").write_text('Mac packaged search marker\n')
    for tool, args, key in (
        (SearchContentTool(root), {'pattern': 'packaged search marker'}, 'matches'),
        (FindFilesTool(root), {'glob': '*.txt'}, 'files'),
    ):
        result = json.loads(tool.execute(ToolCall('mac-package-smoke', tool.name, args)).output)
        assert result[key], result
    owner = TerminalEnvironmentOwner(root, config=TerminalEnvConfig(enable_shell_snapshot=False))
    try:
        environment = owner.build(cwd=root)
        rg = private_ripgrep()
        assert rg.resolve().is_relative_to(package.parent)
        assert environment.values['PATH'].split(os.pathsep)[0] == str(rg.parent)
        assert subprocess.check_output(['/bin/sh', '-c', 'rg --version'], env=environment.values, cwd=root).startswith(b'ripgrep 15.2.0')
    finally:
        owner.close(timeout_seconds=1)
print(json.dumps({'version': version, 'module': str(package), 'interpreter': sys.executable, 'cwd': os.getcwd(), 'isolated': sys.flags.isolated, 'launcher': 'production generator; Chinese/space/apostrophe executable path; version/help passed', 'assets': True, 'filesystemSearch': True, 'terminalPrivateRipgrep': True, 'temporaryCleanup': True}, ensure_ascii=False, indent=2))
