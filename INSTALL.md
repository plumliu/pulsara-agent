# Install Pulsara

[简体中文](INSTALL.zh-CN.md) · [Build from source](DEVELOPMENT.md)

## Download 0.1.1-alpha.1

| System | Installer |
| --- | --- |
| macOS 15+, Apple Silicon | [Download DMG](https://github.com/plumliu/pulsara-agent/releases/download/v0.1.1-alpha.1/Pulsara-0.1.1-alpha.1-macos-arm64.dmg) |
| Windows 11 x64 + WSL2 Ubuntu | [Download EXE](https://github.com/plumliu/pulsara-agent/releases/download/v0.1.1-alpha.1/Pulsara-0.1.1-alpha.1-windows-x64.exe) |

This release includes no native Ubuntu installer. For local amd64 or arm64 builds, follow the [development guide](DEVELOPMENT.md); native Ubuntu desktop acceptance remains deferred.

## macOS

1. Open the DMG and drag Pulsara into Applications.
2. Eject the DMG, then open Pulsara from Applications.
3. If macOS blocks it, use System Settings → Privacy & Security → Open Anyway after checking the download source. This preview has no Developer ID distribution signature or Apple notarization. [Apple instructions](https://support.apple.com/en-us/102445)

## Windows + WSL2

1. If needed, [install WSL2 and Ubuntu](https://learn.microsoft.com/windows/wsl/install). Restart when prompted, open Ubuntu once, and finish creating your Linux user.
2. Run the EXE installer, then open Pulsara from the Start menu. The installer is unsigned; Windows may show an unknown-publisher warning.
3. On first launch, select your WSL2 Ubuntu and connect. Pulsara uses its default non-root user and installs the bundled Linux application resources, preserving interop, automount and other work in the distribution.

Choose project folders under your Linux user's home directory. The Browse button also opens the Windows folder picker at that WSL home; Windows drive paths and folders outside the selected Linux home cannot be used as project directories. Windows attachments can be selected or dropped into the composer.

## Model setup

Add a model connection in Settings, then start a quick session or open a project folder. Python, PostgreSQL, Chromium and search tools are bundled; you do not need uv, Docker or a separate database.

## Data and updates

| Data | Location |
| --- | --- |
| Settings, sessions and database | `~/.pulsara`, or the configured `PULSARA_HOME` |
| Quick-start folders | `<Pulsara home>/workspaces/quick-*` |
| Windows app's Linux resources | `~/.local/share/pulsara/runtime/<version>` in the selected WSL user account |

On Windows, these data paths belong to the Linux user. The Windows app-data directory holds host settings and logs; setting `PULSARA_HOME` in Windows does not select the Linux data home.

Fully quit Pulsara before updating; closing its window leaves it running in the background. On macOS, replace the app in Applications. On Windows, run the new installer. The installer refuses to continue if tasks or database maintenance have not settled.

The Windows uninstaller offers two choices. Both remove the app and its WSL runtime resources:

- **Keep personal data** (default): retain settings, sessions and files.
- **Uninstall completely**: also delete settings, sessions, the database and quick-start files.

Ubuntu and project folders outside the Pulsara data directory are preserved. If WSL cleanup fails, uninstalling stops so you can retry.

**Upgrading from 0.1.0-alpha.5 or earlier:** the database baseline changed in alpha.6, with no automatic migration from those versions. Back up your data first; retaining old sessions requires a separate migration. Do not delete the entire `~/.pulsara` directory.

Development and installed builds share the default data home. Quit one before opening the other. Updates are manual.

## Command line

On macOS, Pulsara registers `~/.local/bin/pulsara` on first launch. Native Ubuntu deb packages manage `/usr/bin/pulsara`. Check the entry in Settings → General → Local services; existing commands are not overwritten and PATH is not modified automatically.

For Windows, use the command path shown in Settings inside a WSL terminal. Launch the desktop app from Windows.

```sh
pulsara --version
pulsara --help
```

Report issues with your OS, version and reproduction steps in [GitHub Issues](https://github.com/plumliu/pulsara-agent/issues). Do not include API keys or OAuth tokens.
