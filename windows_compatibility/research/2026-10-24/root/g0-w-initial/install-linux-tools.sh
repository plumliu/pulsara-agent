set -eux
test "$(id -u)" = 1000
MISE_VERSION=v2026.10.15 sh "$HOME/.cache/pulsara-g0/20261024-environment/mise-install.sh"
export PATH="$HOME/.local/bin:$PATH"
mise --version
cd "$HOME/workspaces/pulsara_agent"
mise trust
mise install
mise exec -- node --version
mise exec -- uv --version
mise exec -- protoc --version
mise exec -- uv python install 3.12.15
mise exec -- uv sync --locked --dev --no-install-project --default-index https://pypi.org/simple
.venv/bin/python --version
git rev-parse HEAD
