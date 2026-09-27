# Formats and passwords

## Choose the path

`python-docx`, `docxtpl`, and the package inspector work on unencrypted DOCX, not binary DOC. Identify the actual format: ordinary DOCX is a ZIP package; binary DOC is usually an OLE container with a `WordDocument` stream; encrypted DOCX commonly uses OLE streams named `EncryptionInfo` and `EncryptedPackage`. A `.doc` filename can also contain RTF or HTML. Let a capable reader identify it; changing the extension does not convert it.

| Input / request | Next step |
| --- | --- |
| Readable `.docx` | Continue the requested reading/editing workflow |
| Legacy `.doc` | Read directly with a DOC-capable reader; choose the editing/output format below |
| Password required to open | Open with the user's supplied password in a capable application; decrypt a separate copy only if the chosen tool needs it |
| Opens but restricts editing | Use the provided editing password in the application's protection controls; file decryption is a different operation |
| Rights-managed document or unsupported encryption | Use the authorized account/native application, or request an accessible copy |

## Choose a working format

- **Reading or extraction:** keep DOC when a capable reader can cover the requested content. Render relevant pages for tables, floating objects, or extraction gaps. Conversion is optional.
- **Local edits with layout preservation:** prefer native Word, when available, to edit a DOC copy and save in DOC. Use a DOC-capable editor; the run/OOXML examples apply to DOCX.
- **DOCX requested, or required by the chosen editing tool:** convert a separate copy and verify it before further edits. Choose a tool that preserves the features relevant to the task. Do not convert merely to use the bundled DOCX helpers.

## DOC ↔ DOCX conversion

For layout-sensitive Word documents, prefer native Word **Save As** when available. Preserve existing compatibility settings where supported unless the task requires upgrading document features. Installed LibreOffice is another conversion option. In a suitable shell, create a fresh output directory for the chosen direction and run:

```sh
mkdir converted-docx
soffice --headless --convert-to 'docx:Office Open XML Text' --outdir converted-docx input.doc
```

```sh
mkdir converted-doc
soffice --headless --convert-to 'doc:MS Word 97' --outdir converted-doc input.docx
```

Export back to DOC only when the task calls for it. Quote real paths containing spaces. On macOS, `soffice` may be `/Applications/LibreOffice.app/Contents/MacOS/soffice`; Windows installations provide `soffice.com`. Use a separate temporary profile via `-env:UserInstallation=file:///absolute/temp/profile` when automating, especially with LibreOffice already open; remove only the profile created for this task after the process exits.

Check for a new, non-empty output, then reopen and [verify it](render-verify.md). DOC export can downgrade modern features; do not promise a lossless round trip. Macro-bearing documents need a macro-capable target and application; preserve them according to the user's scope without executing them.

## Decrypt with the supplied opening password

When the chosen tool needs an unencrypted input, use `msoffcrypto-tool` in the selected task environment. It handles common DOCX Agile/Standard encryption and DOC RC4/RC4 CryptoAPI. Word 95 encryption, DOC XOR obfuscation, and Extensible Encryption are outside this example's support; use Word or another application supporting that exact format. A rejected password and an unsupported format are different outcomes; do not guess passwords.

Optional [decryption example](../examples/decrypt_document.py), which declares its dependency for uv:

```sh
uv run --script /path/to/pulsara-docs/examples/decrypt_document.py locked.docx opened.docx
uv run --script /path/to/pulsara-docs/examples/decrypt_document.py locked.doc opened.doc
```

The interactive prompt does not echo the password. For automation, `--password-stdin` reads one line from a private input pipe, preserving spaces. Supply the user's password through that input channel; keep it out of command arguments, saved scripts, shell history, and logs. If the password is missing or rejected, request the correct password rather than retrying variations.

The example delegates cryptography to the library, verifies the password (and Agile payload integrity), and publishes a separate plaintext file only after decryption and basic format checks succeed. It refuses overwrites. Decryption preserves the underlying format; continue in that format when it supports the task, converting only when the requested output or editing tool requires it. The example supports ordinary DOC/DOCX, not macro-enabled OOXML or templates. Scripts are optional code references; the same library can decrypt into a buffer for reading without saving a plaintext copy.

Reopen and inspect the decrypted content. If only reading was requested, keep any plaintext working copy temporary and remove it after use; if delivering a decrypted file, state that it no longer requires an opening password. Preserve the encrypted original. Do not remove editing restrictions or claim rights-management access was granted merely because decryption succeeded.
