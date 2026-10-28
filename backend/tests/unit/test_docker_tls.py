"""Generated-certificate persistence, renewal, custom overrides and startup fencing."""

from __future__ import annotations

import ipaddress
import os
import ssl
import sys
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from backend.app.core import docker_tls
from backend.app.core.docker_tls import TLSConfigurationError, main, prepare_tls, resolve_tls
from backend.tests.unit.test_docker_healthcheck import certificate


def _public_key(pem):
    return (
        serialization.load_pem_private_key(pem, password=None)
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )


def test_default_resolution_is_https_and_does_not_write(tmp_path):
    files = resolve_tls({"DATA_DIR": str(tmp_path)})
    assert files is not None and files.generated
    assert files.certfile == files.keyfile == tmp_path / "tls" / "server.pem"
    assert not files.certfile.parent.exists()


def test_first_boot_generates_matching_persistent_certificate_and_private_key(tmp_path):
    env = {"DATA_DIR": str(tmp_path), "BAMBUDDY_HTTPS_HOSTNAMES": "bambuddy.home.arpa,192.168.1.42"}
    files = prepare_tls(env)
    assert files is not None
    original = files.certfile.read_bytes()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(files.certfile, files.keyfile)
    assert prepare_tls(env) == files
    assert files.certfile.read_bytes() == original
    cert = x509.load_pem_x509_certificate(original)
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "bambuddy.home.arpa" in names.get_values_for_type(x509.DNSName)
    assert ipaddress.ip_address("192.168.1.42") in names.get_values_for_type(x509.IPAddress)
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    public = (tmp_path / "tls" / "cert.pem").read_bytes()
    assert b"PRIVATE KEY" not in public
    assert x509.load_pem_x509_certificate(public) == cert
    if os.name != "nt":
        assert files.certfile.stat().st_mode & 0o777 == 0o600
        assert files.certfile.parent.stat().st_mode & 0o777 == 0o700


def test_renewal_near_expiry_keeps_private_key(tmp_path):
    now = datetime.now(timezone.utc)
    env = {"DATA_DIR": str(tmp_path)}
    files = prepare_tls(env, now=now)
    original = files.certfile.read_bytes()
    prepare_tls(env, now=now + timedelta(days=340))
    renewed = files.certfile.read_bytes()
    assert renewed != original
    assert _public_key(renewed) == _public_key(original)
    assert x509.load_pem_x509_certificate(renewed).not_valid_after_utc > now + timedelta(days=700)


def test_hostname_change_reissues_certificate_without_changing_key(tmp_path):
    env = {"DATA_DIR": str(tmp_path)}
    files = prepare_tls(env)
    original = files.certfile.read_bytes()
    prepare_tls({**env, "BAMBUDDY_HTTPS_HOSTNAMES": "new.home.arpa"})
    updated = files.certfile.read_bytes()
    assert updated != original and _public_key(updated) == _public_key(original)
    names = (
        x509.load_pem_x509_certificate(updated).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    )
    assert "new.home.arpa" in names.get_values_for_type(x509.DNSName)


def test_custom_certificate_takes_precedence_without_generated_files(tmp_path):
    certfile, keyfile = certificate(tmp_path / "provided")
    original = certfile.read_bytes(), keyfile.read_bytes()
    data = tmp_path / "data"
    files = prepare_tls(
        {"DATA_DIR": str(data), "UVICORN_SSL_CERTFILE": str(certfile), "UVICORN_SSL_KEYFILE": str(keyfile)}
    )
    assert files is not None and not files.generated
    assert not data.exists()
    assert (certfile.read_bytes(), keyfile.read_bytes()) == original


def test_custom_certificate_password_is_supported(tmp_path):
    certfile, keyfile = certificate(tmp_path / "encrypted")
    key = serialization.load_pem_private_key(keyfile.read_bytes(), password=None)
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"synthetic-key-password"),
        )
    )
    assert (
        prepare_tls(
            {
                "UVICORN_SSL_CERTFILE": str(certfile),
                "UVICORN_SSL_KEYFILE": str(keyfile),
                "UVICORN_SSL_KEYFILE_PASSWORD": "synthetic-key-password",
            }
        )
        is not None
    )


def test_bad_custom_certificate_never_falls_back_to_generated_one(tmp_path):
    files = prepare_tls({"DATA_DIR": str(tmp_path)})
    original = files.certfile.read_bytes()
    with pytest.raises(FileNotFoundError):
        prepare_tls({"DATA_DIR": str(tmp_path), "UVICORN_SSL_CERTFILE": str(tmp_path / "missing.pem")})
    assert files.certfile.read_bytes() == original


def test_custom_mismatched_key_is_rejected_without_modifying_files(tmp_path):
    certfile, _ = certificate(tmp_path / "first")
    _, keyfile = certificate(tmp_path / "second")
    original = certfile.read_bytes(), keyfile.read_bytes()
    with pytest.raises(ssl.SSLError):
        prepare_tls({"UVICORN_SSL_CERTFILE": str(certfile), "UVICORN_SSL_KEYFILE": str(keyfile)})
    assert (certfile.read_bytes(), keyfile.read_bytes()) == original


def test_corrupt_managed_pem_is_not_silently_replaced(tmp_path):
    files = prepare_tls({"DATA_DIR": str(tmp_path)})
    files.certfile.write_bytes(b"synthetic corrupt PEM")
    with pytest.raises(ValueError):
        prepare_tls({"DATA_DIR": str(tmp_path)})
    assert files.certfile.read_bytes() == b"synthetic corrupt PEM"


def test_atomic_write_failure_preserves_previous_certificate(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    files = prepare_tls({"DATA_DIR": str(tmp_path)}, now=now)
    original = files.certfile.read_bytes()

    def fail(*args):
        raise OSError("synthetic failure")

    monkeypatch.setattr(docker_tls.os, "replace", fail)
    with pytest.raises(OSError):
        prepare_tls({"DATA_DIR": str(tmp_path)}, now=now + timedelta(days=340))
    assert files.certfile.read_bytes() == original
    assert not list(files.certfile.parent.glob(".https-*"))


def test_opt_out_does_not_create_certificate(tmp_path):
    assert prepare_tls({"DATA_DIR": str(tmp_path), "BAMBUDDY_HTTPS": "false"}) is None
    assert not (tmp_path / "tls").exists()
    with pytest.raises(TLSConfigurationError, match="Remove"):
        resolve_tls({"BAMBUDDY_HTTPS": "false", "UVICORN_SSL_CERTFILE": "synthetic.pem"})


@pytest.mark.parametrize("names", ["bad/name", "bad name", "*.example.test", "https://example.test", "x\n.invalid"])
def test_invalid_names_fail_before_certificate_is_written(tmp_path, names):
    with pytest.raises(TLSConfigurationError):
        prepare_tls({"DATA_DIR": str(tmp_path), "BAMBUDDY_HTTPS_HOSTNAMES": names})
    assert not (tmp_path / "tls" / "server.pem").exists()


def test_launcher_exports_generated_paths_and_execs_original_command(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BAMBUDDY_HTTPS", "true")
    monkeypatch.delenv("UVICORN_SSL_CERTFILE", raising=False)
    monkeypatch.delenv("UVICORN_SSL_KEYFILE", raising=False)
    monkeypatch.delenv("UVICORN_SSL_KEYFILE_PASSWORD", raising=False)
    monkeypatch.setattr(sys, "argv", ["launcher", "uvicorn", "synthetic:app"])
    seen = {}

    def execute(program, argv, env):
        seen.update(program=program, argv=argv, env=env)
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvpe", execute)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 0
    assert seen["program"] == "uvicorn" and seen["argv"] == ["uvicorn", "synthetic:app"]
    assert seen["env"]["UVICORN_SSL_CERTFILE"] == str(tmp_path / "tls" / "server.pem")
    assert seen["env"]["UVICORN_SSL_KEYFILE"] == str(tmp_path / "tls" / "server.pem")


def test_launcher_failure_does_not_echo_private_error_text(monkeypatch, capsys):
    def fail(*args):
        raise OSError("synthetic-private-password")

    monkeypatch.setattr(docker_tls, "prepare_tls", fail)
    monkeypatch.setattr(sys, "argv", ["launcher", "uvicorn", "synthetic:app"])
    assert main() == 1
    assert "synthetic-private-password" not in capsys.readouterr().err
