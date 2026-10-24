set -eux
test "$(id -u)" = 1000
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/workspaces/pulsara_agent"
mise exec -- .venv/bin/python -m pytest -q desktop/scripts/test_build_cache.py desktop/scripts/test_build_platform.py tests/test_desktop_runtime.py tests/test_desktop_launch_directory.py tests/test_desktop_release.py
mise exec -- .venv/bin/python -m ruff check
