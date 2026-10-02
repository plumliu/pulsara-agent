# Ubuntu backend and package validation

The supported build targets are macOS 11+ ARM64 and Ubuntu 24.04 x86_64/ARM64.
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
Ubuntu 24.04 x86_64 and ARM64 runners with PostgreSQL 16 + pgvector and Chromium.
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
The harness creates and drops its own test databases. No host PostgreSQL port
or saved production configuration is used. The cleanup command removes the
container database volume.

Use `DOCKER_DEFAULT_PLATFORM=linux/amd64` or `linux/arm64` to select an
architecture; emulated tests do not replace the native CI matrix.

## Desktop actions

On Ubuntu desktops, install the distribution's desktop components:

```sh
sudo apt-get install zenity xdg-utils
```

Launch Pulsara as your ordinary user inside the graphical session, with its
display and session bus environment inherited. The browser and native dialogs
operate on the machine running Pulsara, not on a remote browser's machine.
Headless containers/SSH sessions do not supply a desktop.

Zenity owns folder selection through GTK's native file chooser (and its portal
integration); Pulsara passes the starting directory as a single argument and
validates the returned local directory. It uses Zenity's supported per-process
exit overrides to distinguish Cancel/Escape from startup failure. The panel
remains open until selected/cancelled or the application shuts down, and requests
are serialized. No Python GTK dependency or copied D-Bus protocol is introduced.
See the [Zenity implementation](https://github.com/GNOME/zenity/blob/4.0.1/src/fileselection.c)
and [exit-code configuration](https://github.com/GNOME/zenity/blob/4.0.1/src/util.c).

[`xdg-open`](https://portland.freedesktop.org/doc/xdg-open.html) owns default-app
and file-manager routing. Opening capability roots, opening a previewed file,
and showing its containing folder share one adapter. Showing a file's location
opens the containing directory on Linux; it does not promise to select the file.
macOS retains its existing native folder picker and Finder selection behavior.
All paths are passed without a shell. Missing components and unavailable desktop
sessions return visible errors, rather than a fake successful action.

Automated tests cover dispatch, cancellation, process cleanup, path validation
and HTTP errors without opening a desktop. Ubuntu GUI/VM checks remain manual:
select/cancel a working directory, open `.agents`/Pulsara home, and open/show a
previewed file with your desktop's default applications. Verify both X11 and
Wayland when available; backend/package gates do not certify window appearance.
