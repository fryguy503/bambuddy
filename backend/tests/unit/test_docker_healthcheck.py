"""Real loopback HTTP/TLS probes; no printer, account or Docker daemon needed."""

from __future__ import annotations

import ssl
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from backend.app.core.docker_healthcheck import HealthCheckError, check, main
from backend.app.core.docker_tls import TLSConfigurationError, prepare_tls


def certificate(directory, *, expired=False):
    directory.mkdir()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bambuddy.example.test")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=-1 if expired else 1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("bambuddy.example.test")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = directory / "cert.pem", directory / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return certfile, keyfile


@contextmanager
def endpoint(*, tls=None, status=200):
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*tls)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    worker.start()
    try:
        yield {"HOST": "0.0.0.0", "PORT": str(server.server_port), "BAMBUDDY_HTTPS": "false"}, paths
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        assert not worker.is_alive()


def tls_environment(environ, files):
    return {
        **environ,
        "BAMBUDDY_HTTPS": "true",
        "UVICORN_SSL_CERTFILE": str(files[0]),
        "UVICORN_SSL_KEYFILE": str(files[1]),
    }


def test_http_health_uses_app_port_and_ignores_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    with endpoint() as (environ, paths):
        check(environ)
    assert paths == ["/health"]


def test_https_accepts_exact_self_signed_certificate_with_external_dns_name(tmp_path):
    files = certificate(tmp_path / "valid")
    with endpoint(tls=files) as (environ, paths):
        check(tls_environment(environ, files))
    assert paths == ["/health"]


def test_https_certificate_mismatch_sends_no_http_request(tmp_path):
    actual = certificate(tmp_path / "actual")
    expected = certificate(tmp_path / "expected")
    with endpoint(tls=actual) as (environ, paths), pytest.raises(HealthCheckError, match="does not match"):
        check(tls_environment(environ, expected))
    assert paths == []


def test_https_rejects_expired_certificate_before_sending_request(tmp_path):
    files = certificate(tmp_path / "expired", expired=True)
    with endpoint(tls=files) as (environ, paths), pytest.raises(HealthCheckError, match="validity period"):
        check(tls_environment(environ, files))
    assert paths == []


def test_https_fullchain_uses_leaf_as_exact_pin(tmp_path):
    leaf = certificate(tmp_path / "leaf")
    other = certificate(tmp_path / "other")
    fullchain = tmp_path / "fullchain.pem"
    fullchain.write_bytes(leaf[0].read_bytes() + other[0].read_bytes())
    with endpoint(tls=leaf) as (environ, paths):
        check(tls_environment(environ, (fullchain, leaf[1])))
    assert paths == ["/health"]


def test_key_without_certificate_does_not_fall_back_to_http():
    with endpoint() as (environ, paths), pytest.raises(TLSConfigurationError, match="Configure UVICORN_SSL_CERTFILE"):
        check({**environ, "BAMBUDDY_HTTPS": "true", "UVICORN_SSL_KEYFILE": "/synthetic/unused.pem"})
    assert paths == []


def test_https_combined_certificate_and_key_preserves_uvicorn_configuration(tmp_path):
    files = certificate(tmp_path / "combined-source")
    combined = tmp_path / "combined.pem"
    combined.write_bytes(files[0].read_bytes() + files[1].read_bytes())
    with endpoint(tls=(combined, None)) as (environ, paths):
        check({**environ, "BAMBUDDY_HTTPS": "true", "UVICORN_SSL_CERTFILE": str(combined)})
    assert paths == ["/health"]


def test_unhealthy_http_status_is_rejected():
    with endpoint(status=503) as (environ, _), pytest.raises(HealthCheckError, match="HTTP 200"):
        check(environ)


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_invalid_port_fails(port):
    with pytest.raises(ValueError):
        check({"PORT": port})


def test_missing_certificate_diagnostic_does_not_echo_paths(monkeypatch, capsys):
    monkeypatch.setenv("BAMBUDDY_HTTPS", "true")
    monkeypatch.setenv("UVICORN_SSL_CERTFILE", "/private/synthetic-name/cert.pem")
    monkeypatch.setenv("UVICORN_SSL_KEYFILE", "/private/synthetic-name/key.pem")
    assert main() == 1
    error = capsys.readouterr().err
    assert "FileNotFoundError" in error
    assert "synthetic-name" not in error


def test_main_reports_success_for_healthy_http(monkeypatch):
    monkeypatch.setenv("BAMBUDDY_HTTPS", "false")
    monkeypatch.delenv("UVICORN_SSL_CERTFILE", raising=False)
    monkeypatch.delenv("UVICORN_SSL_KEYFILE", raising=False)
    with endpoint() as (environ, _):
        for name, value in environ.items():
            monkeypatch.setenv(name, value)
        assert main() == 0


def test_generated_certificate_health_uses_persistent_path_without_exported_env(tmp_path):
    settings = {"DATA_DIR": str(tmp_path), "BAMBUDDY_HTTPS": "true"}
    files = prepare_tls(settings)
    assert files is not None
    with endpoint(tls=(files.certfile, files.keyfile)) as (environ, paths):
        check({**environ, **settings})
    assert paths == ["/health"]


def test_passive_health_check_never_generates_missing_certificate(tmp_path):
    with pytest.raises(FileNotFoundError):
        check({"DATA_DIR": str(tmp_path), "BAMBUDDY_HTTPS": "true"})
    assert not (tmp_path / "tls").exists()
