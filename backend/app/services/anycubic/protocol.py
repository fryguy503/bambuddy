"""Independent implementation of the signed Kobra S1 LAN wire protocol.

Protocol corroboration: chrisfore/anycubic_ha_local research/PROTOCOL-VALIDATED.md
and the user's independently developed Anycubic Orca plugin/native-code audit.
No vendor binaries, keys, frontend resources or third-party implementation copied.
"""

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

PREFIX = "anycubic/anycubicCloud/v1"
MAX_BYTES = 256 * 1024
MODEL_ID = "20025"
_SEGMENT = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class AnycubicError(ValueError):
    """A fixed public diagnostic; never include a transport's secret-bearing error."""


def private_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise AnycubicError("A private IP address is required") from None
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
    if not any(address in ipaddress.ip_network(net) for net in networks):
        raise AnycubicError("A private IP address is required")
    return str(address)


def fingerprint(value: str) -> str:
    if not isinstance(value, str):
        raise AnycubicError("An approved TLS certificate fingerprint is required")
    value = value.replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise AnycubicError("An approved TLS certificate fingerprint is required")
    return value


def segment(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not _SEGMENT.fullmatch(str(value)):
        raise AnycubicError("Invalid printer identity")
    return str(value)


def json_object(raw: bytes) -> dict:
    if not raw or len(raw) > MAX_BYTES:
        raise AnycubicError("Printer response exceeds its size limit")
    try:
        value = json.loads(raw, parse_constant=lambda _: None)
    except (ValueError, UnicodeError, RecursionError):
        raise AnycubicError("Invalid printer response") from None
    if not isinstance(value, dict):
        raise AnycubicError("Invalid printer response")
    return value


def _fetch(host: str, method: str, path: str) -> dict:
    """Direct bounded HTTP; no proxy, redirects or device-selected destination."""
    connection = http.client.HTTPConnection(host, 18910, timeout=5)
    deadline = time.monotonic() + 8
    try:
        connection.request(method, path)
        response = connection.getresponse()
        if response.status != 200:
            raise AnycubicError("Printer LAN handshake failed")
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AnycubicError("Printer LAN handshake timed out")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            block = response.read1(min(16384, MAX_BYTES + 1 - size))
            if not block:
                break
            chunks.append(block)
            size += len(block)
            if size > MAX_BYTES:
                raise AnycubicError("Printer response exceeds its size limit")
        return json_object(b"".join(chunks))
    except (OSError, http.client.HTTPException):
        raise AnycubicError("Printer LAN handshake failed") from None
    finally:
        connection.close()


def get_info(host: str) -> dict:
    host = private_host(host)
    info = _fetch(host, "GET", "/info")
    if info.get("ctrlType") != "lan":
        raise AnycubicError("Enable LAN mode on the printer")
    if (info.get("modelId") is not None and str(info["modelId"]) != MODEL_ID) or (
        info.get("modelId") is None and info.get("modelName") != "Anycubic Kobra S1"
    ):
        raise AnycubicError("Only the Kobra S1 is supported by this experimental provider")
    identity(info)
    token = info.get("token")
    if not isinstance(token, str) or len(token) < 32 or len(token) > 128 or not token.isascii():
        raise AnycubicError("Invalid LAN handshake token")
    try:
        url = urlsplit(info.get("ctrlInfoUrl", ""))
        valid = (
            url.scheme == "http"
            and url.hostname == host
            and url.port == 18910
            and url.path == "/ctrl"
            and not url.query
            and not url.fragment
            and url.username is None
            and url.password is None
        )
    except ValueError:
        valid = False
    if not valid:
        raise AnycubicError("Printer returned an unexpected LAN endpoint")
    return info


def identity(info):
    """Older S1 firmware exposes a USN rather than a physical serial in /info."""
    serial = info.get("cn")
    if isinstance(serial, str) and serial and len(serial) <= 128 and not any(ord(c) < 32 for c in serial):
        return serial
    usn = info.get("usn")
    if isinstance(usn, str) and re.fullmatch(r"uuid:fdm:(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", usn):
        return usn
    raise AnycubicError("Printer identity was not reported")


def inspect_fingerprint(host: str) -> str:
    """Certificate-only probe, without pairing or MQTT credentials."""
    host = private_host(host)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        with (
            socket.create_connection((host, 9883), timeout=5) as stream,
            context.wrap_socket(stream, server_hostname=host) as secured,
        ):
            certificate = secured.getpeercert(binary_form=True)
            if not certificate:
                raise AnycubicError("Printer did not provide a TLS certificate")
            return hashlib.sha256(certificate).hexdigest()
    except OSError:
        raise AnycubicError("Printer TLS certificate could not be inspected") from None


@dataclass(repr=False)
class Credentials:
    host: str
    model_id: str
    device_id: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    certificate: str = field(default="", repr=False)
    private_key: str = field(default="", repr=False)


def pair(host: str, serial_number: str, approved_fingerprint: str) -> Credentials:
    """Obtain ephemeral broker credentials, bound to the selected printer and pin."""
    host = private_host(host)
    pin = fingerprint(approved_fingerprint)
    if not hmac.compare_digest(inspect_fingerprint(host), pin):
        raise AnycubicError("Printer TLS certificate changed")
    info = get_info(host)
    token = info["token"]
    timestamp = str(int(time.time() * 1000))
    nonce = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(6))
    first = hashlib.md5(token[:16].encode(), usedforsecurity=False).hexdigest()
    sign = hashlib.md5((first + timestamp + nonce).encode(), usedforsecurity=False).hexdigest()
    query = urlencode({"ts": timestamp, "nonce": nonce, "sign": sign, "did": secrets.token_hex(16)})
    response = _fetch(host, "POST", "/ctrl?" + query)
    if response.get("code") != 200 or not isinstance(response.get("data"), dict):
        raise AnycubicError("Printer rejected LAN pairing")
    data = response["data"]
    try:
        iv = data["token"].encode()
        cipher = base64.b64decode(data["info"], validate=True)
        if cipher.startswith(b"Salted__"):
            cipher = cipher[16:]
        decryptor = Cipher(algorithms.AES(token[16:32].encode()), modes.CBC(iv)).decryptor()
        padded = decryptor.update(cipher) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        decrypted = json_object(unpadder.update(padded) + unpadder.finalize())
        broker = urlsplit(decrypted["broker"])
        if (
            broker.scheme not in {"mqtts", "ssl"}
            or broker.hostname != host
            or broker.port != 9883
            or broker.path not in {"", "/"}
            or broker.query
            or broker.fragment
            or broker.username is not None
            or broker.password is not None
        ):
            raise ValueError
        model_id = segment(decrypted.get("modeId", info.get("modelId")))
        device_id = segment(decrypted["deviceId"])
        username, password = decrypted["username"], decrypted["password"]
        if model_id != MODEL_ID or any(not isinstance(s, str) or not s or len(s) > 4096 for s in (username, password)):
            raise ValueError
        certificate, private_key = decrypted.get("devicecrt", ""), decrypted.get("devicepk", "")
        if any(not isinstance(s, str) or len(s) > 65536 for s in (certificate, private_key)) or bool(
            certificate
        ) != bool(private_key):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise AnycubicError("Invalid encrypted LAN pairing response") from None
    if serial_number not in {identity(info), device_id}:
        raise AnycubicError("Printer identity does not match")
    return Credentials(host, model_id, device_id, username, password, certificate, private_key)


class _PinnedSocket(ssl.SSLSocket):
    """Verify before any first application bytes, even without an explicit handshake."""

    def _authenticate(self):
        if getattr(self, "_pin_verified", False):
            return
        self.do_handshake()

    def _verify_peer(self):
        certificate = self.getpeercert(binary_form=True)
        context = self.context
        if (
            not isinstance(context, PinnedTLSContext)
            or not certificate
            or not hmac.compare_digest(hashlib.sha256(certificate).hexdigest(), context.pin)
        ):
            self.close()
            raise ssl.SSLCertVerificationError("Printer TLS certificate does not match the approved fingerprint")
        parsed = x509.load_der_x509_certificate(certificate)
        now = time.time()
        if not parsed.not_valid_before_utc.timestamp() <= now <= parsed.not_valid_after_utc.timestamp():
            self.close()
            raise ssl.SSLCertVerificationError("Printer TLS certificate is outside its validity period")
        self._pin_verified = True

    def do_handshake(self, *args, **kwargs):
        super().do_handshake(*args, **kwargs)
        self._verify_peer()

    def send(self, *args, **kwargs):
        self._authenticate()
        return super().send(*args, **kwargs)

    def sendall(self, *args, **kwargs):
        self._authenticate()
        return super().sendall(*args, **kwargs)

    def write(self, *args, **kwargs):
        self._authenticate()
        return super().write(*args, **kwargs)

    def recv(self, *args, **kwargs):
        self._authenticate()
        return super().recv(*args, **kwargs)

    def recv_into(self, *args, **kwargs):
        self._authenticate()
        return super().recv_into(*args, **kwargs)

    def read(self, *args, **kwargs):
        self._authenticate()
        return super().read(*args, **kwargs)


class PinnedTLSContext(ssl.SSLContext):
    """Exact approved certificate replaces unusable printer PKI; no global TLS changes."""

    def __new__(cls, *_args, **_kwargs):
        return super().__new__(cls, ssl.PROTOCOL_TLS_CLIENT)

    def __init__(self, approved_fingerprint, host=None, credentials=None):
        self.pin = fingerprint(approved_fingerprint)
        self.check_hostname = False
        self.verify_mode = ssl.CERT_NONE
        self.minimum_version = ssl.TLSVersion.TLSv1_2
        self.sslsocket_class = _PinnedSocket
        self.bound_host = private_host(host) if host else None
        if credentials is not None and credentials.certificate:
            from .private_files import PrivateDirectory

            with PrivateDirectory() as directory:
                cert_path = directory.write("client.pem", credentials.certificate.encode())
                key_path = directory.write("client-key.pem", credentials.private_key.encode())
                self.load_cert_chain(str(cert_path), str(key_path))

    def wrap_socket(self, sock, *args, **kwargs):
        if self.bound_host and kwargs.get("server_hostname") != self.bound_host:
            raise ssl.SSLCertVerificationError("Printer TLS fingerprint belongs to another address")
        return super().wrap_socket(sock, *args, **kwargs)
