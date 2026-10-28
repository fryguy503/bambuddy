"""Persistent Docker HTTPS defaults, shared by startup and passive health checks."""

from __future__ import annotations

import ipaddress
import os
import re
import ssl
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_GENERATED_MARKER = "urn:bambuddy:docker-generated-https:v1"
_RENEW_BEFORE = timedelta(days=30)
_MAX_PEM_BYTES = 1024 * 1024


class TLSConfigurationError(ValueError):
    """A fixed diagnostic without private key material or password values."""


@dataclass(frozen=True)
class TLSFiles:
    certfile: Path
    keyfile: Path | None
    generated: bool = False


def resolve_tls(environ: Mapping[str, str] | None = None) -> TLSFiles | None:
    """Resolve the same listener configuration without generating or writing files."""
    environ = os.environ if environ is None else environ
    flag = environ.get("BAMBUDDY_HTTPS", "true").lower()
    if flag not in {"true", "1", "yes", "on", "false", "0", "no", "off"}:
        raise TLSConfigurationError("BAMBUDDY_HTTPS must be true or false.")
    certfile = environ.get("UVICORN_SSL_CERTFILE") or ""
    keyfile = environ.get("UVICORN_SSL_KEYFILE") or ""
    if flag in {"false", "0", "no", "off"}:
        if certfile or keyfile:
            raise TLSConfigurationError("Remove the UVICORN_SSL certificate settings when BAMBUDDY_HTTPS=false.")
        return None
    if keyfile and not certfile:
        raise TLSConfigurationError("Configure UVICORN_SSL_CERTFILE when using UVICORN_SSL_KEYFILE.")
    if certfile:
        return TLSFiles(Path(certfile), Path(keyfile) if keyfile else None)
    if environ.get("UVICORN_SSL_KEYFILE_PASSWORD"):
        raise TLSConfigurationError("A private-key password requires a supplied HTTPS certificate.")
    combined = Path(environ.get("DATA_DIR") or "/app/data") / "tls" / "server.pem"
    return TLSFiles(combined, combined, generated=True)


def _read_pem(path: Path) -> bytes:
    with path.open("rb") as stream:
        pem = stream.read(_MAX_PEM_BYTES + 1)
    if len(pem) > _MAX_PEM_BYTES:
        raise TLSConfigurationError("The HTTPS PEM file is too large.")
    return pem


def _names(environ: Mapping[str, str]) -> list[x509.GeneralName]:
    values = ["localhost", "bambuddy", "127.0.0.1", "::1"]
    extra = environ.get("BAMBUDDY_HTTPS_HOSTNAMES", "")
    if len(extra) > 4096:
        raise TLSConfigurationError("Too many HTTPS hostnames.")
    values.extend(item.strip() for item in extra.split(",") if item.strip())
    if len(values) > 36:
        raise TLSConfigurationError("Configure at most 32 additional HTTPS hostnames or IP addresses.")
    result: list[x509.GeneralName] = [x509.UniformResourceIdentifier(_GENERATED_MARKER)]
    for value in values:
        try:
            name: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(value))
        except ValueError:
            try:
                dns = value.rstrip(".").encode("idna").decode("ascii").lower()
            except UnicodeError:
                raise TLSConfigurationError("HTTPS hostnames must be valid DNS names or IP addresses.") from None
            if len(dns) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in dns.split(".")
            ):
                raise TLSConfigurationError("HTTPS hostnames must be valid DNS names or IP addresses.")
            name = x509.DNSName(dns)
        if name not in result:
            result.append(name)
    return result


def _write_atomic(path: Path, value: bytes, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".https-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _public_key(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _generated(files: TLSFiles, environ: Mapping[str, str], now: datetime) -> None:
    directory = files.certfile.parent
    if directory.is_symlink() or files.certfile.is_symlink():
        raise TLSConfigurationError("The managed HTTPS certificate path must not be a symlink.")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    names = _names(environ)
    certificate = None
    if files.certfile.exists():
        pem = _read_pem(files.certfile)
        certificate = x509.load_pem_x509_certificate(pem)
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or _public_key(key) != certificate.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        ):
            raise TLSConfigurationError("The managed HTTPS certificate and private key do not match.")
        existing_names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if x509.UniformResourceIdentifier(_GENERATED_MARKER) not in existing_names:
            raise TLSConfigurationError(
                "Supply external certificates through UVICORN_SSL_CERTFILE and UVICORN_SSL_KEYFILE."
            )
        certificate.verify_directly_issued_by(certificate)
        files.certfile.chmod(0o600)
        renew = (
            certificate.not_valid_after_utc <= now + _RENEW_BEFORE
            or certificate.not_valid_before_utc > now
            or set(existing_names) != set(names)
        )
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        renew = True
    if renew:
        identity = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Bambuddy")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(identity)
            .issuer_name(identity)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256())
        )
        combined = certificate.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        # A single atomic PEM replacement keeps the certificate and key paired.
        _write_atomic(files.certfile, combined, 0o600)
    if certificate is None:
        raise TLSConfigurationError("The managed HTTPS certificate could not be prepared.")
    # Exportable public certificate; the private key stays in server.pem.
    public = certificate.public_bytes(serialization.Encoding.PEM)
    public_path = directory / "cert.pem"
    if not public_path.is_file() or _read_pem(public_path) != public:
        _write_atomic(public_path, public, 0o644)


def prepare_tls(environ: Mapping[str, str] | None = None, *, now: datetime | None = None) -> TLSFiles | None:
    environ = os.environ if environ is None else environ
    files = resolve_tls(environ)
    if files is None:
        return None
    now = datetime.now(timezone.utc) if now is None else now
    if files.generated:
        _generated(files, environ, now)
    certificate = x509.load_pem_x509_certificate(_read_pem(files.certfile))
    if not certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc:
        raise TLSConfigurationError("The HTTPS certificate is outside its validity period.")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(files.certfile, files.keyfile, password=environ.get("UVICORN_SSL_KEYFILE_PASSWORD") or None)
    return files


def main() -> int:
    command = sys.argv[1:]
    if not command:
        print("[entrypoint] No application command was provided.", file=sys.stderr)
        return 1
    environ = os.environ.copy()
    try:
        files = prepare_tls(environ)
        if files:
            environ["UVICORN_SSL_CERTFILE"] = str(files.certfile)
            if files.keyfile:
                environ["UVICORN_SSL_KEYFILE"] = str(files.keyfile)
            else:
                environ.pop("UVICORN_SSL_KEYFILE", None)
            source = "persistent self-signed" if files.generated else "supplied"
            print(f"[entrypoint] HTTPS enabled with a {source} certificate.", flush=True)
        else:
            print("[entrypoint] BAMBUDDY_HTTPS=false; HTTP enabled.", flush=True)
        os.execvpe(command[0], command, environ)
    except TLSConfigurationError as error:
        print(f"[entrypoint] HTTPS configuration failed: {error}", file=sys.stderr)
    except Exception as error:
        print(
            f"[entrypoint] HTTPS startup failed ({type(error).__name__}); check certificate files and permissions.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
