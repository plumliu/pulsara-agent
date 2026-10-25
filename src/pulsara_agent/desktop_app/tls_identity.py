"""One process-owned loopback TLS identity, carried as a complete public value."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import ip_address
import os
from pathlib import Path
import ssl
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@dataclass(frozen=True)
class DesktopTlsIdentity:
    context: ssl.SSLContext
    certificate_pem: str


def create_desktop_tls_identity(private_directory: Path) -> DesktopTlsIdentity:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Pulsara private desktop")])
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        # Physical lifetime is this process. X509 dates must not cap long tasks
        # or require a kernel restart after a host clock change.
        .not_valid_before(datetime(1970, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc))
        .add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ip_address("127.0.0.1")), x509.DNSName("localhost"),
        ]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem = certificate.public_bytes(serialization.Encoding.PEM)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with tempfile.TemporaryDirectory(prefix=".tls-", dir=private_directory) as temporary:
        paths = []
        for filename, value in (
            ("certificate.pem", pem),
            ("key.pem", key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
        ):
            path = Path(temporary) / filename
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
            paths.append(path)
        context.load_cert_chain(paths[0], paths[1])
    return DesktopTlsIdentity(context, pem.decode("ascii"))
