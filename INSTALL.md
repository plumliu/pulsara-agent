# Install Pulsara

[简体中文](INSTALL.zh-CN.md) · [Build from source](DEVELOPMENT.md)

## Download 0.2.0-alpha.3

| System | Installer |
| --- | --- |
| macOS 15+, Apple Silicon | [Download DMG](https://github.com/plumliu/pulsara-agent/releases/download/v0.2.0-alpha.3/Pulsara-0.2.0-alpha.3-macos-arm64.dmg) |

The 0.2.0-alpha.3 release provides a macOS Apple Silicon DMG. For Windows + WSL and Ubuntu source builds, see the [development guide](DEVELOPMENT.md).

## macOS

1. Open the DMG and drag Pulsara into Applications.
2. Eject the DMG, then open Pulsara from Applications.
3. If macOS blocks it, use System Settings → Privacy & Security → Open Anyway after checking the download source. This preview has an ad-hoc signature covering the app and bundled resources; it has no Developer ID distribution signature or Apple notarization. A valid ad-hoc signature does not establish Gatekeeper trust. [Apple instructions](https://support.apple.com/en-us/102445)

If macOS reports that the application is damaged, download the current DMG again. Include the output of `codesign --verify --deep --strict --verbose=2 /Applications/Pulsara.app` in an issue if verification still fails.

macOS separately controls access to Desktop, Documents and other protected folders. Choose the folders you need in System Settings → Privacy & Security → Files & Folders. [Apple file-access guidance](https://support.apple.com/guide/mac-help/mchld5a35146/mac). After re-signing, an existing Full Disk Access grant may no longer match the app. If system logs report a code-requirement mismatch, remove the old entry and add the current Pulsara app to renew that grant. Full Disk Access covers the entire disk; choose the scope you need.

## Windows + WSL2

The following setup applies to a Windows package built from source.

1. If needed, [install WSL2 and Ubuntu](https://learn.microsoft.com/windows/wsl/install). Restart when prompted, open Ubuntu once, and finish creating your Linux user.
2. Run the EXE installer, then open Pulsara from the Start menu. The installer is unsigned; Windows may show an unknown-publisher warning.
3. On first launch, select your WSL2 Ubuntu and connect. Pulsara uses its default non-root user and installs the bundled Linux application resources, preserving interop, automount and other work in the distribution.

Choose project folders under your Linux user's home directory. The Browse button also opens the Windows folder picker at that WSL home; Windows drive paths and folders outside the selected Linux home cannot be used as project directories. Windows attachments can be selected or dropped into the composer.

## Model setup

Add a model connection in Settings, then start a quick session or open a project folder. Python, PostgreSQL, Chromium and search tools are bundled; you do not need uv, Docker or a separate database.

MCP tool search uses BM25 out of the box. When Settings → Models has a complete Embedding connection, Pulsara sends search terms and the visible tools' public names and descriptions to that endpoint for semantic search. It stores tool vectors in its local database. Input suggestions and history search for recall default to off. You can enable either in Settings → Models.

## Data and updates

| Data | Location |
| --- | --- |
| Settings, sessions and database | `~/.pulsara`, or the configured `PULSARA_HOME` |
| Quick-start folders | `<Pulsara home>/workspaces/quick-*` |
| Windows app's Linux resources | `~/.local/share/pulsara/runtime/<version>` in the selected WSL user account |

On Windows, these data paths belong to the Linux user. The Windows app-data directory holds host settings and logs; setting `PULSARA_HOME` in Windows does not select the Linux data home.

Fully quit Pulsara before updating; closing its window leaves it running in the background. On macOS, replace the app in Applications. On Windows, run the new installer. The installer refuses to continue if tasks or database maintenance have not settled.

**Upgrading from 0.2.0-alpha.2:** The MCP vector cache changes the database baseline. Existing databases initialized by the published alpha.2 build require a full local reset to use alpha.3. Save any data you need to keep before confirming the reset.

The Windows uninstaller offers two choices. Both remove the app and its WSL runtime resources:

- **Keep personal data** (default): retain settings, sessions and files.
- **Uninstall completely**: also delete settings, sessions, the database and quick-start files.

Ubuntu and project folders outside the Pulsara data directory are preserved. Explicit uninstall requests a normal Pulsara shutdown, then terminates verified application processes if they do not respond and stops its dedicated database before cleanup. This can interrupt unfinished work; it never terminates the WSL distribution. If process exit cannot be confirmed or cleanup fails, uninstall reports the error and stops for retry.

**Incompatible local data after upgrading:** The startup page explains the incompatibility. Select the permanent-deletion checkbox above the buttons, then click “Reset and start over”. Pulsara stops its dedicated database, clears the current personal data and initializes the current database before reopening the workbench. No old data is migrated. This removes model connections, API keys, authorizations, sessions, memory, skill/plugin configuration, attachments and quick-start files inside the data home. Configure your models again afterwards. Project files outside that home remain untouched; save any files you need before confirming. Ordinary network, permission or database-occupancy errors do not offer this reset action.

Development and installed builds share the default data home. Quit one before opening the other. Updates are manual.

## Command line

On macOS, Pulsara registers `~/.local/bin/pulsara` on first launch. Native Ubuntu deb packages manage `/usr/bin/pulsara`. Check the entry in Settings → General → Local services; existing commands are not overwritten and PATH is not modified automatically.

For Windows, use the command path shown in Settings inside a WSL terminal. Launch the desktop app from Windows.

```sh
pulsara --version
pulsara --help
```

Report issues with your OS, version and reproduction steps in [GitHub Issues](https://github.com/plumliu/pulsara-agent/issues). Do not include API keys or OAuth tokens.
