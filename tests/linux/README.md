# Ubuntu backend and package validation

The Python wheel build targets are macOS ARM64 and Ubuntu x86_64/ARM64.
Ubuntu release numbers are not enforced for native or WSL builds. The pinned CI
and container baselines remain test environments, not release-number gates.
The macOS wheel tag does not establish the minimum OS version of Electron or
other bundled desktop binaries.
Linux wheels use `linux_x86_64` / `linux_aarch64` tags; this is not a manylinux
compatibility claim. Official ripgrep 15.2.0 musl assets are prepared explicitly,
verified by their pinned upstream SHA-256, then checked with binutils `readelf`
for ELF64 architecture and absence of a loader/shared-library dependency.
Hatch builds stay offline. Build wheel and sdist directly from the prepared
source with `uv build --offline --no-build-isolation --sdist --wheel`. The sdist
excludes native binaries: consumers unpack it, explicitly run
`tools/prepare_ripgrep.py`, then build the wheel. The default uv build path
(wheel from an unprepared sdist) intentionally fails. Install `binutils` on Linux
build hosts; wheel users do not need binutils, a system `rg`, or Node.js.

`.github/workflows/ubuntu-tests.yml` runs the complete Python suite on native
Ubuntu 24.04 x86_64 and ARM64 runners with PostgreSQL 17, pg_search 0.26.1,
pgvector and Chromium. Both workflows and the local test Compose use the pinned
`paradedb/paradedb:0.26.1-pg17` image, matching the root Compose configuration.
The public GitHub runners have [four CPUs](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
CI uses two pytest workers to leave CPU capacity for ParadeDB's BM25 work and
Chromium while running the complete suite with the existing synchronization
deadlines. Additional tests wait for a worker rather than being excluded.
Integration modules each receive a fresh migrated database: pg_search's global
corpus statistics and index work must not carry across unrelated test files.
The suite includes PTY/process cancellation, local MCP, hooks, Skill/Plugin
publication, search, HTTP, storage and canonical conversation behavior. It also
checks wheel/editable/sdist builds. The final gate installs the actual wheel in
a clean environment and runs `installed_package_smoke.py` outside the checkout,
without a source `PYTHONPATH`. Built frontend assets ship inside the wheel.
These tests use fixture models, not paid production model calls.

For the same suite in a local Ubuntu container, run from the repository root:

```sh
docker compose -f tests/linux/compose.yaml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f tests/linux/compose.yaml down --volumes
```

Compose supplies an isolated PostgreSQL service, initializes its runtime role,
waits for database readiness, and gives the test container Docker's init process.
Orphan descendants must be reaped by PID 1 for the existing physical process-group
completion contract. For a manual `docker run`, always use `--init`. Running
pytest as a non-reaping PID 1 leaves zombie groups and fails the terminal tests;
Pulsara does not replace the OS/init owner's reaping responsibility.
The harness creates test databases from PostgreSQL's pristine `template0`, so
extensions preinstalled in ParadeDB's `template1` do not leak into migration
fixtures, and drops those databases after use. No host PostgreSQL port
or saved production configuration is used. The cleanup command removes the
container database volume.

Use `DOCKER_DEFAULT_PLATFORM=linux/amd64` or `linux/arm64` to select an
architecture; emulated tests do not replace the native CI matrix.

## Desktop application checks

The product entry point is the Electron desktop application. Its preload and
main process own native file/folder dialogs, file-manager reveal, and opening
supported files with their associated applications. The former Python
Zenity/AppleScript/default-app adapters and HTTP directory-picker endpoint have
been removed.

Launch the installed application as an ordinary user inside a graphical session
with its display and session bus environment. Headless containers and SSH
sessions do not supply desktop acceptance. The installed app starts its own
bundled PostgreSQL instance; the Compose database above is an explicit backend
test fixture, not a product setup requirement.

Build instructions and native resource requirements are in the root
[README](../../README.md#build-the-desktop-from-source). Desktop shell tests run
with `npm --prefix desktop test`; Python lifecycle tests exercise the private
host channel, owner lock, database initialization and ordinary shutdown owner.
Neither suite substitutes for running the installed Electron artifact.

Ubuntu GUI/VM checks cover the application icon in the Dock, Alt+Tab and tray,
title-bar controls, close/return/quit, directory and
file selection, real-path drag/drop, capability directories, native file open
and reveal, independent preview tabs, history navigation, and a real model task.
Check the desktop environment that is actually available and record it. Backend
or wheel checks do not certify the window, tray, packaged native extensions, or
an untested Linux desktop. The full scoped acceptance contract is in the
[desktop plan](../../PULSARA_DESKTOP_MIGRATION_AND_BUNDLED_POSTGRESQL_PLAN.zh.md).
