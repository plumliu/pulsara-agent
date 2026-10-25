"""Run only the original Windows context scenario against a newly installed wheel.

The neighboring adapters reuse original dogfood owners and recording. Their
database arguments must identify a separate approved disposable cluster.
"""
import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import runpy
import sys
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    bootstrap, remaining = parser.parse_known_args()
    import pulsara_agent

    repo = bootstrap.repo.resolve(strict=True)
    installed = bootstrap.installed_root.resolve(strict=True)
    assert repo == Path("/home/plumwsl/workspaces/pulsara_agent")
    assert Path(sys.executable).is_relative_to(installed)
    assert Path(pulsara_agent.__file__).is_relative_to(installed) and "site-packages" in Path(pulsara_agent.__file__).parts
    product = Path(pulsara_agent.__file__).parent
    for relative in ("conversation_kernel/provider_dispatch.py", "hooks/executor.py", "conversation_kernel/mcp/sdk_facade.py"):
        assert (product / relative).read_bytes() == (repo / "src/pulsara_agent" / relative).read_bytes(), relative
    # Product was imported from the installed wheel before exposing test helpers.
    sys.path.append(str(repo))
    own_directory = Path(__file__).resolve().parent
    sys.path.append(str(own_directory))
    sys.argv = [str(own_directory / "run_remaining_wsl.py"), *remaining,
                "--kind", "subagent", "--cases", "context"]
    if "--execute" not in remaining:
        runpy.run_path(str(own_directory / "run_remaining_wsl.py"), run_name="__main__")
        return
    assert "--output" in remaining and "--interop-helper" in remaining
    output = Path(remaining[remaining.index("--output") + 1])
    provenance_path = output.parent / "r5-provenance.json"
    assert not provenance_path.exists()
    output.parent.mkdir(parents=True, exist_ok=True)
    installed_helper = Path("/mnt/c/Users/plumlocal/AppData/Local/Programs/pulsara/resources/windows-interop/pulsara-interop.exe")
    assert Path(remaining[remaining.index("--interop-helper") + 1]) == installed_helper
    assert installed_helper.is_file() and os.access(installed_helper, os.X_OK)
    provenance = {"python": sys.executable, "product": str(product), "version": version("pulsara-agent"),
                  "complete_source_bytes_equal": ["conversation_kernel/provider_dispatch.py", "hooks/executor.py", "conversation_kernel/mcp/sdk_facade.py"],
                  "installed_helper": str(installed_helper), "runner_domains": []}

    def save():
        provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")

    import pulsara_agent.windows_interop as interop
    from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
    from pulsara_agent.capability.pulsara_home import require_pulsara_home
    from pulsara_agent.desktop_app.linux_storage import require_native_ext4
    configure = interop.configure_interop
    initialize = ConversationKernelRunner.__init__

    def configured(**values):
        assert values == {"helper_path": str(installed_helper), "distribution": "Ubuntu", "user": "plumwsl"}
        configure(**values)
        actual = interop._configuration
        assert actual.helper_path == installed_helper and actual.python_executable == Path(sys.executable)
        provenance["actual_interop_configuration"] = {"helper": str(actual.helper_path), "distribution": actual.distribution,
                                                       "user": actual.user, "python": str(actual.python_executable), "uid": os.getuid()}
        save()

    def initialized(owner, *args, **kwargs):
        private_home = require_pulsara_home()
        assert private_home != Path("/home/plumwsl/.pulsara")
        require_native_ext4(private_home)
        assert interop._configuration is not None
        provenance["runner_domains"].append({"home": str(private_home), "uid": os.getuid(), "platform": sys.platform,
                                               "interop_configured": interop._configuration is not None})
        save()
        initialize(owner, *args, **kwargs)

    save()
    with patch.object(interop, "configure_interop", configured), patch.object(ConversationKernelRunner, "__init__", initialized):
        runpy.run_path(str(own_directory / "run_remaining_wsl.py"), run_name="__main__")


if __name__ == "__main__":
    main()
