# /// script
# requires-python = ">=3.10"
# dependencies = ["msoffcrypto-tool>=5.4,<6"]
# ///
"""Decrypt DOC/DOCX with a supplied opening password; never overwrite the input."""
from __future__ import annotations

import argparse
import getpass
import io
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile

import msoffcrypto
from msoffcrypto import exceptions


def decrypt(source: Path, output: Path, password: str) -> Path:
    if output.exists() or source.resolve() == output.resolve():
        raise FileExistsError("Choose a separate, unused output file")
    if not password:
        raise ValueError("Provide the document's opening password")
    plaintext = io.BytesIO()
    with source.open("rb") as stream:
        document = msoffcrypto.OfficeFile(stream)
        if not document.is_encrypted():
            raise ValueError("Input is not encrypted; use the ordinary document workflow")
        if document.format == "ooxml":
            document.load_key(password=password, verify_password=True)
            document.decrypt(plaintext, verify_integrity=True)
            with zipfile.ZipFile(plaintext) as package:
                types = ET.fromstring(package.read("[Content_Types].xml"))
                word_type = (
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document.main+xml"
                )
                if not any(item.get("ContentType") == word_type for item in types):
                    raise ValueError("Expected ordinary DOCX; use a format-specific workflow")
                if package.testzip() is not None:
                    raise ValueError("Decrypted package failed its ZIP integrity check")
            suffix = ".docx"
        elif document.format == "doc97":
            # DOC verifies the password in load_key; it has no OOXML keyword flags.
            document.load_key(password=password)
            if getattr(document, "type", None) not in {"rc4", "rc4_cryptoapi"}:
                raise exceptions.DecryptionError("Unsupported DOC encryption; use a native editor")
            document.decrypt(plaintext)
            plaintext.seek(0)
            reopened = msoffcrypto.OfficeFile(plaintext)
            if reopened.format != "doc97" or reopened.is_encrypted():
                raise ValueError("Decrypted output is not an unencrypted DOC")
            suffix = ".doc"
        else:
            raise ValueError("Expected an encrypted DOC or DOCX document")
    if output.suffix.lower() != suffix:
        raise ValueError(f"Decryption preserves the format; choose an output ending in {suffix}")
    # The library owns decryption. This adapter avoids its CLI's eager 'wb' output
    # opening, so a wrong password cannot truncate a file or leave a partial result.
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(plaintext.getbuffer())
    except OSError:
        output.unlink()
        raise
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--password-stdin", action="store_true", help="Read one password line from stdin")
    args = parser.parse_args()
    if args.password_stdin:
        password = sys.stdin.readline().removesuffix("\n").removesuffix("\r")
    elif sys.stdin.isatty():
        password = getpass.getpass("Opening password: ")
    else:
        parser.error("Use an interactive terminal or --password-stdin with a private input pipe")
    try:
        output = decrypt(args.input, args.output, password)
    except exceptions.InvalidKeyError:
        parser.exit(1, "Password verification or payload integrity check failed; check the password and file.\n")
    except (OSError, ValueError, exceptions.FileFormatError, exceptions.DecryptionError,
            zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        parser.exit(1, f"Decryption failed: {exc}\n")
    print(output.resolve())


if __name__ == "__main__":
    main()
