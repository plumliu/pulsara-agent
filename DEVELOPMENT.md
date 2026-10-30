# Build Pulsara

[简体中文](DEVELOPMENT.zh-CN.md) · [Installation](INSTALL.md) · [Downloads: 0.1.1-alpha.2](https://github.com/plumliu/pulsara-agent/releases/tag/v0.1.1-alpha.2)

Read [AGENTS.md](AGENTS.md) before changing code. Build native resources on the target OS; Windows uses a Linux payload built in WSL2 Ubuntu and an Electron shell built on Windows.

MCP networking, optional resource-template discovery and real-model acceptance follow the [MCP host-network contract](PULSARA_MCP_HOST_NETWORK_AND_OPTIONAL_TEMPLATES_IMPLEMENTATION_SPEC.zh.md).

## Build tools

`mise.toml` pins Node.js 22.23.3, uv 0.13.4 and protoc 34.0. `.python-version` pins Python 3.12.15; uv manages the repository `.venv`.

- Native builds: a C compiler, make, pkg-config, Bison and Flex.
- macOS 15+ Apple Silicon: Xcode command-line tools and `brew bundle --file desktop/Brewfile`.
- Ubuntu: binutils, patchelf, xz-utils and libopenblas0, plus the packages below. Ubuntu release numbers are not enforced; actual native dependency compatibility still matters.
- Windows: the system .NET Framework compiler, used to build the interop helper.

Resources include PostgreSQL 17.9, pgvector 0.8.1, pg_search 0.26.1, Python and Playwright Chromium. The pg_search packages target macOS ARM64 Sequoia and Ubuntu Noble PG17 x86_64/ARM64.

## macOS

From the repository root:

```sh
brew bundle --file desktop/Brewfile
mise trust
mise install
mise run --jobs 1 desktop:package
```

The DMG is written to `desktop/release`. For an unpacked app, run `mise run --jobs 1 desktop:verify`, then `mise exec -- npm --prefix desktop run package:dir`. Signing and notarization require separate configuration.

## Windows + WSL2

Use two checkouts of the same commit. Keep the Linux checkout on WSL's ext4 filesystem and the Windows checkout on Windows. Each has its own `.venv` and `node_modules`.

In WSL2 Ubuntu, after installing the native build dependencies:

```sh
mise trust
mise install
mise run --jobs 1 desktop:verify
mise run --jobs 1 desktop:wsl-payload
```

Copy `desktop/build/wsl-payload/payload.tar` and `manifest.json` into `desktop/resources/wsl-payload/` in the Windows checkout. Then run in PowerShell:

```powershell
mise trust
mise install
mise run --jobs 1 desktop:windows-package
```

The EXE is written to `desktop/release/Pulsara-<version>-windows-x64.exe`. The build verifies the payload version and checksum and compiles the Windows helper. User setup and data locations are covered in [INSTALL.md](INSTALL.md); the remaining acceptance matrix is in [windows_compatibility](windows_compatibility/README.md).

## Native Ubuntu

No Ubuntu deb is published for 0.1.1-alpha.2. Native x64/arm64 desktop acceptance remains deferred. To build locally, install mise and run as a normal user:

```sh
sudo apt update
sudo apt install -y build-essential bison flex pkg-config binutils patchelf \
  libopenblas0 xz-utils ca-certificates curl git libyaml-0-2 libcrypt1
mise trust
mise install
mise run desktop:bootstrap
mise exec -- .venv/bin/python -m playwright install-deps chromium
mise run --jobs 1 desktop:package
sudo apt install ./desktop/release/Pulsara-0.1.1-alpha.2-linux-amd64.deb
```

Use the arm64 filename on ARM64. A container build uses the same task graph:

```sh
docker buildx build --platform linux/arm64 --target installer \
  --output type=local,dest=desktop/release/linux-arm64 \
  -f desktop/build-environment.Dockerfile .
```

For amd64, use `linux/amd64` and a separate output directory. Docker is a build option, not a runtime requirement.

## Incremental builds and checks

Keep `desktop/build`, `desktop/resources` and mise's cache between builds. Application changes rebuild the renderer and wheel; unchanged native dependencies are reused. Changed recipes, tools, SDKs or system libraries invalidate their resource tasks. Standard `desktop:verify` and `desktop:package` tasks include resource smoke checks.

To rebuild one resource, use `mise run --force desktop:postgres` or `mise run --force desktop:python-dependencies`. Custom PostgreSQL extensions use `mise run --force desktop:postgres -- --extension-lib /path/to/lib --extension-share /path/to/extension`; rerun it when those inputs change. Do not patch SDK caches or alter payload checksums to reuse stale resources.

```sh
mise exec -- .venv/bin/python -m pytest -m 'not postgres'
mise exec -- npm --prefix frontend test
mise exec -- npm --prefix desktop test
mise exec -- npm --prefix desktop run test:startup
mise exec -- .venv/bin/python -m pytest -q desktop/scripts/test_build_cache.py desktop/scripts/test_build_platform.py
```

Database fixtures and native Linux checks: [tests/linux/README.md](tests/linux/README.md). Use `.venv/Scripts/python.exe` for Python commands on Windows. To run the development shell with prepared resources, use `mise exec -- npm --prefix desktop start`.

Use `mise exec -- npm --prefix desktop run test:windows` for Windows host desktop tests. This shares the Windows CI entry point and checks shell font packaging. The full desktop suite above also exercises POSIX scripts, permissions and symlinks; run it in full on macOS or WSL/Linux.

The Electron archive includes only startup fonts (Inter, Noto Sans SC and Gelasio). Workbench fonts, including all MiSans weights, live in the Python package renderer assets; Windows carries these in its WSL payload. Keep the startup page independent of WSL, and do not add workbench-only fonts to the Electron archive.

## Releases

Version 0.1.1-alpha.2 ships locally built macOS ARM64 DMG and Windows x64 EXE installers. Application code comes from tag `v0.1.1-alpha.2`; Windows test maintenance does not change runtime behavior. Python uses version `0.1.1a2`; Settings displays `0.1.1`.

GitHub Actions are manual. The existing Native Desktop Packages workflow and `release_artifacts.py` assembly still require a DMG, two Ubuntu debs and a Windows EXE; this macOS/Windows release does not use that assembly flow. Upload each local installer to its matching Release without replacing existing assets. Keep published tags fixed.

Check version consistency with `mise exec -- uv run --no-sync python desktop/scripts/release_artifacts.py check --tag v0.1.1-alpha.2`.
