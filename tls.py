"""Self-signed TLS for iPhone Safari (cleartext LAN is blocked).

Pattern adapted from MiniMax webapp: cert + key under certs/, /trust page
serves the PEM so Safari can install it once.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
import socket
from pathlib import Path

CERT_DIR = Path(os.environ.get("IMAGINE_CERT_DIR", Path(__file__).resolve().parent / "certs"))
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"


def _default_sans() -> list[str]:
    hosts = [
        "localhost",
        "127.0.0.1",
        "spark-7819.tail0182f5.ts.net",
        "spark-7819.local",
        "100.123.177.66",
    ]
    extra = os.environ.get("IMAGINE_TLS_HOSTS", "")
    if extra:
        hosts.extend(h.strip() for h in extra.split(",") if h.strip())
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for h in hosts:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def ensure_certs(force: bool = False) -> tuple[Path, Path]:
    """Create self-signed cert/key if missing. Returns (cert, key) paths."""
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    if CERT_FILE.exists() and KEY_FILE.exists() and not force:
        return CERT_FILE, KEY_FILE

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as e:
        raise SystemExit(
            "cryptography is required for TLS. pip install cryptography"
        ) from e

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Imagine Spark")]
    )
    san_list: list[x509.GeneralName] = []
    for h in _default_sans():
        try:
            san_list.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            san_list.append(x509.DNSName(h))
    # also include hostname if resolvable
    try:
        san_list.append(x509.DNSName(socket.gethostname()))
    except Exception:
        pass

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
        .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .sign(key, hashes.SHA256())
    )

    KEY_FILE.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    KEY_FILE.chmod(0o600)
    CERT_FILE.chmod(0o644)
    return CERT_FILE, KEY_FILE


def ssl_kwargs() -> dict:
    cert, key = ensure_certs()
    return {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
