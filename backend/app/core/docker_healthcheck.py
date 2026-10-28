"""Container readiness over HTTP, or HTTPS pinned to its mounted certificate.

Uvicorn already reads UVICORN_SSL_CERTFILE / UVICORN_SSL_KEYFILE. The local
probe uses that public certificate as its exact trust anchor, including for
self-signed certificates or certificates whose DNS name isn't localhost.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import os
import ssl
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path


class HealthCheckError(ValueError):
    """A safe diagnostic for the container's health log."""


def _certificate_pin(path: str) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    with Path(path).open("rb") as stream:
        pem = stream.read(1024 * 1024 + 1)
    if len(pem) > 1024 * 1024:
        raise HealthCheckError("The HTTPS certificate file is too large.")
    # The first certificate in Uvicorn's PEM file is its leaf certificate;
    # subsequent certificates can supply the intermediate chain to browsers.
    certificate = x509.load_pem_x509_certificate(pem)
    now = datetime.now(timezone.utc)
    if not certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc:
        raise HealthCheckError("The HTTPS certificate is outside its validity period.")
    return hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).digest()


def check(environ: Mapping[str, str] | None = None, *, timeout: float = 5) -> None:
    """Probe the app's own listener, bypassing HTTP proxy environment variables."""
    environ = os.environ if environ is None else environ
    host = environ.get("HOST") or "127.0.0.1"
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    port = int(environ.get("PORT") or "8000")
    if not 1 <= port <= 65535:
        raise HealthCheckError("PORT must be between 1 and 65535.")
    certfile = environ.get("UVICORN_SSL_CERTFILE") or ""
    keyfile = environ.get("UVICORN_SSL_KEYFILE") or ""
    if keyfile and not certfile:
        raise HealthCheckError("Configure UVICORN_SSL_CERTFILE when using UVICORN_SSL_KEYFILE.")
    # Uvicorn also accepts a combined certificate/private-key PEM with only
    # SSL_CERTFILE set. Do not mistake that valid configuration for HTTP.

    connection: http.client.HTTPConnection
    if certfile:
        expected = _certificate_pin(certfile)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # Exact certificate pinning below replaces CA/hostname verification
        # for this local probe only, before sending the HTTP request. Browser
        # certificate validation and outbound HTTPS clients are unchanged.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    else:
        expected = None
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.connect()
        if expected is not None:
            if not isinstance(connection.sock, ssl.SSLSocket):
                raise HealthCheckError("The health endpoint did not establish TLS.")
            presented = connection.sock.getpeercert(binary_form=True)
            if not presented or not hmac.compare_digest(hashlib.sha256(presented).digest(), expected):
                raise HealthCheckError("The health endpoint's certificate does not match the mounted certificate.")
        connection.request("GET", "/health")
        response = connection.getresponse()
        if response.status != 200:
            raise HealthCheckError("The health endpoint did not return HTTP 200.")
    finally:
        connection.close()


def main() -> int:
    try:
        check()
    except HealthCheckError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        # Paths, certificate contents and transport exception text do not
        # belong in Docker's externally readable health log.
        print(f"Bambuddy health check failed ({type(error).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
