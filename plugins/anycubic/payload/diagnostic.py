"""Bounded LAN diagnostics. Never starts a print, upload, motion or camera stream."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import math
import ssl
import time
from typing import Any, Literal

import aiohttp

from backend.app.printers.contracts import PrinterConfig, PrinterConnection
from backend.app.printers.sdk import ConnectionDiagnosticResult, DiagnosticCheck, network_diagnostic_checks

from .client import AnycubicMQTTClient
from .files import upload_url
from .models import get_model
from .protocol import MAX_BYTES, fingerprint, identity, json_object, model_from_info, private_host, validate_info

PORT_TIMEOUT = 4.0
INFO_TIMEOUT = 8.0
OBSERVATION_SECONDS = 8.0
MAX_OBSERVATION_SECONDS = 15.0
CLEANUP_SECONDS = 30.0
CheckStatus = Literal["pass", "fail", "warn", "skip"]


def check(
    id: str, status: CheckStatus, title: str, message: str, hint: str | None = None, **params: Any
) -> DiagnosticCheck:
    return DiagnosticCheck(id=id, status=status, title=title, message=message, hint=hint, params=params)


async def _close(writer) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), timeout=1)


async def _tcp(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=PORT_TIMEOUT)
        await _close(writer)
        return True
    except Exception:
        return False


async def _certificate(host: str) -> str | None:
    # Inspect the certificate without trusting it or sending MQTT credentials.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 9883, ssl=context, server_hostname=host, ssl_handshake_timeout=PORT_TIMEOUT),
            timeout=PORT_TIMEOUT,
        )
        try:
            secured = writer.get_extra_info("ssl_object")
            certificate = secured.getpeercert(binary_form=True) if secured else None
            return hashlib.sha256(certificate).hexdigest() if certificate else None
        finally:
            await _close(writer)
    except Exception:
        return None


async def _info(host: str) -> dict | None:
    authority = f"[{host}]" if ":" in host else host
    try:
        async with (
            aiohttp.ClientSession(
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=INFO_TIMEOUT, sock_connect=PORT_TIMEOUT, sock_read=PORT_TIMEOUT),
            ) as session,
            session.get(f"http://{authority}:18910/info", allow_redirects=False) as response,
        ):
            if response.status != 200:
                return None
            body = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    return None
            return json_object(bytes(body))
    except Exception:
        return None


async def _network_advice(host: str) -> list[DiagnosticCheck]:
    try:
        return await asyncio.wait_for(network_diagnostic_checks(host), timeout=INFO_TIMEOUT)
    except Exception:
        return [check("host_network_advice", "warn", "Host network", "Host network advice could not be completed.")]


def _dispatch_hold_check(config: PrinterConfig, connection: PrinterConnection | None) -> DiagnosticCheck:
    if config.printer_id is None:
        return check(
            "anycubic_dispatch_holds",
            "skip",
            "Previous dispatch holds",
            "Saved-printer dispatch state was not inspected for this unsaved connection.",
        )
    try:
        from .dispatch_status import read

        refresh = getattr(connection, "refresh_dispatch_holds", None)
        if connection is not None and getattr(connection, "config", None) == config and callable(refresh):
            holds = refresh()
        else:
            holds = read(config)
        if not isinstance(holds, list):
            raise ValueError("Dispatch status is unavailable")
    except Exception:
        return check(
            "anycubic_dispatch_holds",
            "fail",
            "Previous dispatch holds",
            "The durable dispatch state could not be read; no held operation was changed.",
            "An operator should inspect the saved printer's dispatch state before trying another print.",
        )
    return check(
        "anycubic_dispatch_holds",
        "fail" if holds else "pass",
        "Previous dispatch holds",
        "A previous native dispatch is still held." if holds else "No previous native dispatch is held.",
        "An operator can inspect dispatch holds, then use Advanced controls → Resolve uncertain job only after verifying the printer is idle. Held jobs are never automatically resent or cleared."
        if holds
        else None,
        count=len(holds),
    )


def _native(connection: PrinterConnection | None) -> AnycubicMQTTClient | None:
    native = getattr(connection, "native", None) if connection is not None else None
    return native if isinstance(native, AnycubicMQTTClient) else None


def _new_client(config: PrinterConfig, pin: str) -> AnycubicMQTTClient:
    # Use no application callbacks, journal, inventory accounting or job adoption.
    return AnycubicMQTTClient(config.ip_address, config.serial_number, model=config.model, tls_fingerprint=pin)


def _observed(native: AnycubicMQTTClient) -> tuple[bool, bool]:
    with native._lock:
        authenticated = bool(native._client is not None and native._subscribed and not native._stop.is_set())
        fresh = bool(
            authenticated
            and native.state.connected
            and native.report_messages_since_connect > 0
            and native._last_state_time
            and 0 <= time.monotonic() - native._last_state_time <= native.STALE_TIMEOUT
        )
    return authenticated, fresh


async def _observe(native: AnycubicMQTTClient, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _observed(native)[1] or native.last_connect_error:
            return
        await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))


async def _cleanup(native: AnycubicMQTTClient) -> None:
    # A cancelled async wait cannot stop the native pairing thread. Keep ownership
    # until its bounded HTTP/TLS work and disconnect/join have finished.
    cleanup = asyncio.create_task(asyncio.to_thread(native.disconnect, timeout=CLEANUP_SECONDS))
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        while not cleanup.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(cleanup)
        with contextlib.suppress(Exception):
            cleanup.result()
        raise


def _session_checks(native: AnycubicMQTTClient, config: PrinterConfig) -> list[DiagnosticCheck]:
    authenticated, fresh = _observed(native)
    checks = [
        check(
            "anycubic_mqtt_auth",
            "pass" if authenticated else "fail",
            "LAN pairing and MQTT authentication",
            "The current LAN session is authenticated and subscribed."
            if authenticated
            else "An authenticated MQTT subscription was not observed.",
            None
            if authenticated
            else "Check LAN mode and the approved printer identity and certificate. Pairing secrets are never displayed.",
        ),
        check(
            "printer_publishing",
            "pass" if fresh else "warn",
            "Printer telemetry",
            "Fresh printer state was observed."
            if fresh
            else "Fresh usable printer state was not observed within the check window.",
            None
            if fresh
            else "Keep the printer powered on in LAN mode, then retry. An open port alone does not prove telemetry is publishing.",
        ),
    ]
    endpoint = False
    if authenticated and fresh and native._upload_url:
        with contextlib.suppress(Exception):
            endpoint = bool(upload_url(config.ip_address, native._upload_url))
    checks.append(
        check(
            "anycubic_files",
            "pass" if endpoint else "warn" if authenticated else "skip",
            "File service readiness",
            "The printer advertised a validated LAN upload endpoint; no upload was attempted."
            if endpoint
            else "A usable file upload endpoint has not been advertised in this session.",
            None
            if endpoint
            else "File operations need an authenticated connection and the printer's LAN file service.",
        )
    )
    with native._lock:
        detected = [box for box in native._telemetry.boxes if box >= 0]
    required = config.options.get("require_ace") is True
    material_fresh = bool(detected and native.ace_material_snapshot() is not None)
    checks.append(
        check(
            "anycubic_ace",
            "pass" if material_fresh else "warn" if detected else "fail" if required else "skip",
            "ACE material system",
            "Fresh ACE material information was observed."
            if material_fresh
            else "An ACE unit was detected, but its material information is stale or incomplete."
            if detected
            else "A required ACE unit was not detected."
            if required
            else "No optional ACE unit was detected.",
            None
            if material_fresh or not detected and not required
            else "Check ACE power and its printer connection, then refresh the printer's material information.",
            units=len(detected),
        )
    )
    return checks


async def diagnose(
    config: PrinterConfig,
    *,
    connection: PrinterConnection | None = None,
    wait_for_publish_seconds: float = 0,
    progress: dict | None = None,
) -> ConnectionDiagnosticResult:
    checks: list[DiagnosticCheck] = []
    if progress is not None:
        progress.update(checks=checks, stage="ports")

    def result() -> ConnectionDiagnosticResult:
        overall = (
            "problems"
            if any(item.status == "fail" for item in checks)
            else "warnings"
            if any(item.status == "warn" for item in checks)
            else "ok"
        )
        return ConnectionDiagnosticResult(
            printer_id=config.printer_id, ip_address=config.ip_address, overall=overall, checks=checks
        )

    try:
        host = private_host(config.ip_address)
    except Exception:
        checks.append(
            check(
                "anycubic_address",
                "fail",
                "Printer LAN address",
                "The target is not a supported private LAN address.",
                "Enter the printer's private LAN IP address.",
            )
        )
        return result()
    native = _native(connection)
    checks.append(await asyncio.to_thread(_dispatch_hold_check, config, connection))
    connection_matches = native is None or (
        getattr(connection, "config", None) == config
        and native.ip_address == host
        and native.serial_number == config.serial_number
        and get_model(native.model) == get_model(config.model)
    )
    info, mqtt, certificate, camera, advisory = await asyncio.gather(
        _info(host), _tcp(host, 9883), _certificate(host), _tcp(host, 18088), _network_advice(host)
    )
    checks.extend(advisory)
    checks.extend(
        [
            check(
                "anycubic_http_info",
                "pass" if info is not None else "fail",
                "LAN identity service",
                "The HTTP identity service responded."
                if info is not None
                else "The LAN identity service did not return usable information.",
                None if info is not None else "Enable LAN mode and allow TCP port 18910 from the Bambuddy host.",
                port=18910,
            ),
            check(
                "anycubic_mqtt_tcp",
                "pass" if mqtt else "fail",
                "MQTT port",
                "The MQTT TCP port is reachable." if mqtt else "The MQTT TCP port did not respond.",
                None if mqtt else "Allow TCP port 9883 between the Bambuddy host and printer.",
                port=9883,
            ),
            check(
                "anycubic_tls",
                "pass" if certificate else "fail",
                "MQTT TLS",
                "The printer presented a TLS certificate."
                if certificate
                else "A TLS certificate could not be inspected.",
                None if certificate else "Check LAN mode and TLS access to port 9883.",
            ),
        ]
    )
    info_valid = model_matches = identity_matches = False
    if info is not None:
        try:
            validate_info(host, info)
            info_valid = True
        except Exception:
            pass
        try:
            detected_model = model_from_info(info)
            model_matches = get_model(config.model) == detected_model
        except Exception:
            pass
        try:
            reported_identity = identity(info)
            identity_matches = config.serial_number == reported_identity or bool(
                native is not None and connection_matches and native._device_id == config.serial_number
            )
        except Exception:
            pass
    checks.extend(
        [
            check(
                "anycubic_lan_mode",
                "pass" if info_valid else "fail" if info is not None else "skip",
                "LAN mode handshake",
                "The printer advertises a supported LAN handshake."
                if info_valid
                else "A supported LAN-mode handshake was not verified.",
                None if info_valid else "Enable the printer's LAN mode and review its discovered identity.",
            ),
            check(
                "anycubic_model",
                "pass" if model_matches else "fail" if info is not None else "skip",
                "Printer model",
                "The reported model matches the configured printer."
                if model_matches
                else "The reported model was unsupported or did not match the selected model.",
            ),
            check(
                "anycubic_identity",
                "pass" if identity_matches and connection_matches else "fail" if info is not None else "skip",
                "Printer identity",
                "The reported identity matches the configured printer."
                if identity_matches and connection_matches
                else "The target identity could not be matched to this printer.",
                None
                if identity_matches and connection_matches
                else "Review the identity from LAN discovery before attempting pairing.",
            ),
        ]
    )
    pin = None
    supplied_pin = config.options.get("tls_fingerprint")
    if isinstance(supplied_pin, str):
        with contextlib.suppress(Exception):
            pin = fingerprint(supplied_pin)
    pin_matches = bool(pin and certificate and hmac.compare_digest(pin, certificate))
    if native is not None:
        connection_matches = bool(connection_matches and pin and hmac.compare_digest(native.tls_fingerprint, pin))
        checks.append(
            check(
                "anycubic_connection_binding",
                "pass" if connection_matches else "fail",
                "Live connection configuration",
                "The live connection belongs to this configuration and approved certificate."
                if connection_matches
                else "The live connection belongs to a different configuration and was not reused.",
                None
                if connection_matches
                else "Refresh the saved printer connection after changing its model, address or certificate, then retry.",
            )
        )
    checks.append(
        check(
            "anycubic_tls_pin",
            "pass" if pin_matches else "warn" if not supplied_pin else "fail",
            "Approved printer certificate",
            "The TLS certificate matches the approved fingerprint."
            if pin_matches
            else "No approved TLS fingerprint is configured."
            if not supplied_pin
            else "The TLS certificate could not be matched to the approved fingerprint.",
            None
            if pin_matches
            else "Review the printer's certificate through trusted LAN discovery. This diagnostic never trusts a new certificate automatically.",
        )
    )
    can_authenticate = bool(
        mqtt and pin_matches and info_valid and model_matches and identity_matches and connection_matches
    )
    can_observe_live = bool(
        native is not None
        and connection_matches
        and pin_matches
        and (info is None or model_matches and identity_matches)
    )
    owned: AnycubicMQTTClient | None = None
    if progress is not None:
        progress["stage"] = "printer_publishing"
    try:
        if can_authenticate or can_observe_live:
            seconds = float(wait_for_publish_seconds)
            seconds = min(MAX_OBSERVATION_SECONDS, max(0, seconds)) if math.isfinite(seconds) else 0
            if native is None:
                owned = native = _new_client(config, pin or "")
                native.connect()
                await _observe(native, seconds or OBSERVATION_SECONDS)
            elif seconds:
                await _observe(native, seconds)
            checks.extend(_session_checks(native, config))
        else:
            checks.extend(
                [
                    check(
                        "anycubic_mqtt_auth",
                        "skip",
                        "LAN pairing and MQTT authentication",
                        "Authentication was skipped because prerequisite identity, model, port or certificate checks did not pass.",
                    ),
                    check(
                        "printer_publishing",
                        "skip",
                        "Printer telemetry",
                        "Telemetry was not tested without a verified printer connection.",
                    ),
                    check(
                        "anycubic_files",
                        "skip",
                        "File service readiness",
                        "File readiness was not tested without a verified printer connection.",
                    ),
                    check(
                        "anycubic_ace",
                        "skip",
                        "ACE material system",
                        "Accessory information was not tested without a verified printer connection.",
                    ),
                ]
            )
    except Exception:
        checks.append(
            check(
                "anycubic_mqtt_auth",
                "fail",
                "LAN pairing and MQTT authentication",
                "The isolated LAN connection check could not be completed.",
                "Verify LAN mode, printer identity and the approved certificate, then retry.",
            )
        )
    finally:
        if owned is not None:
            if progress is not None:
                progress["stage"] = "cleanup"
            try:
                await _cleanup(owned)
            except Exception:
                checks.append(
                    check(
                        "anycubic_cleanup",
                        "warn",
                        "Temporary diagnostic connection",
                        "The temporary diagnostic connection could not confirm a clean shutdown.",
                    )
                )
    checks.append(
        check(
            "anycubic_camera_tcp",
            "pass" if camera else "skip",
            "Optional camera port",
            "The camera TCP port responded; no stream or video frame was requested or verified."
            if camera
            else "The optional camera port did not respond; no camera was activated.",
            port=18088,
        )
    )
    if progress is not None:
        progress["stage"] = "done"
    return result()
