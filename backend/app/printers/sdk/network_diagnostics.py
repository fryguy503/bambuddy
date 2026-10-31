"""Bounded, read-only observations about the host's route to one printer.

These checks do not scan, resolve names, send printer commands, or establish
whether a printer service works. Container isolation and routed subnets are
advisories: both can support perfectly healthy unicast printer connections.
"""

import asyncio
import ipaddress
import json
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from backend.app.services.discovery import OCI_RUNTIMES, detect_container_runtime
from backend.app.services.network_utils import get_network_interfaces

from .diagnostics import DiagnosticCheck

_PROBE_TIMEOUT_SECONDS = 1.0
_COMMAND_TIMEOUT_SECONDS = 0.5
_SOCKET_TIMEOUT_SECONDS = 0.5
_T = TypeVar("_T")


async def _probe(callback: Callable[[], _T]) -> tuple[bool, _T | None]:
    """Keep blocking platform APIs off the event loop and bound the caller's wait."""
    try:
        return True, await asyncio.wait_for(asyncio.to_thread(callback), timeout=_PROBE_TIMEOUT_SECONDS)
    except Exception:
        return False, None


def _route_source(destination: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    family = socket.AF_INET if destination.version == 4 else socket.AF_INET6
    # UDP connect only consults the local route table; no send/sendto occurs.
    with socket.socket(family, socket.SOCK_DGRAM) as connection:
        connection.settimeout(_SOCKET_TIMEOUT_SECONDS)
        connection.connect((str(destination), 1))
        return str(ipaddress.ip_address(connection.getsockname()[0]))


def _native_interface_present() -> bool:
    for index, name in socket.if_nameindex():
        if name == "lo" or "/" in name or "\\" in name:
            continue
        interface = Path("/sys/class/net") / name
        if (interface / "tun_flags").exists():
            continue
        try:
            own_index = (interface / "ifindex").read_text().strip()
            peer_index = (interface / "iflink").read_text().strip()
        except (OSError, ValueError):
            continue
        if own_index == str(index) and own_index == peer_index:
            return True
    return False


def _network_mode() -> str | None:
    """Conservative Linux namespace evidence, without runtime socket access."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        result = subprocess.run(
            ["ip", "-d", "-j", "link", "show"],
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
        links = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        links = None
    if isinstance(links, list):
        kinds = []
        for link in links:
            if not isinstance(link, dict) or link.get("ifname") == "lo":
                continue
            info = link.get("linkinfo")
            if not isinstance(info, dict):
                kinds.append(None)
                continue
            kind = info.get("info_kind")
            if kind == "macvlan":
                return "macvlan"
            if kind == "ipvlan":
                data = info.get("info_data")
                mode = data.get("mode") if isinstance(data, dict) else None
                return "ipvlan_l3" if mode in ("l3", "l3s") else "ipvlan"
            kinds.append(kind)
        if _native_interface_present():
            return "host"
        if kinds and all(kind == "veth" for kind in kinds):
            return "bridge"
    elif _native_interface_present():
        return "host"
    return None


def _source_network(source: str, interfaces: list[dict]) -> tuple[str, str] | None:
    """Use only a real prefix from the interface owning the chosen source."""
    source_address = ipaddress.ip_address(source)
    for interface in interfaces:
        try:
            address = ipaddress.ip_address(interface["ip"])
            if address != source_address:
                continue
            prefix = interface.get("netmask")
            subnet = interface.get("subnet")
            if prefix:
                network = ipaddress.ip_network(f"{source_address}/{prefix}", strict=False)
            elif isinstance(subnet, str) and "/" in subnet:
                network = ipaddress.ip_network(subnet, strict=False)
            else:
                continue
            if network.version != source_address.version or source_address not in network:
                continue
            return str(interface.get("name") or "unknown"), str(network)
        except (KeyError, TypeError, ValueError):
            continue
    return None


async def network_diagnostic_checks(ip_address: str) -> list[DiagnosticCheck]:
    """Describe this host's environment and local route, without contacting services."""
    try:
        destination = ipaddress.ip_address(ip_address)
    except ValueError:
        return [
            DiagnosticCheck(
                id="network_address",
                status="skip",
                title="Printer network address",
                message="A literal IP address is needed to inspect the local route. This check does not resolve hostnames.",
                hint="Check the printer address in its connection settings.",
            )
        ]

    runtime_result, route_result, interfaces_result = await asyncio.gather(
        _probe(detect_container_runtime),
        _probe(lambda: _route_source(destination)),
        _probe(lambda: get_network_interfaces(include_excluded=True)),
    )
    runtime_known, runtime = runtime_result
    route_known, source = route_result
    interfaces_known, interfaces = interfaces_result
    checks = [
        DiagnosticCheck(
            id="environment",
            status="pass" if runtime_known else "skip",
            title="Application environment",
            params={"runtime": runtime, "container": bool(runtime), "platform": sys.platform} if runtime_known else {},
            message=(
                f"The application is running in {runtime}."
                if runtime
                else "No supported container-runtime marker was found."
            )
            if runtime_known
            else "The application environment could not be inspected within the time limit.",
        )
    ]

    mode_known, mode = await _probe(_network_mode) if runtime in OCI_RUNTIMES else (True, None)
    if mode:
        advisory = mode in ("bridge", "ipvlan_l3")
        checks.append(
            DiagnosticCheck(
                id="network_mode",
                status="warn" if advisory else "pass",
                title="Container networking",
                params={"runtime": runtime, "mode": mode},
                message="Isolated or routed container networking was observed. Broadcast discovery may be limited; direct printer connections can still work."
                if advisory
                else f"Host-style or direct LAN interfaces are visible ({mode}). This observation does not test printer connectivity.",
                hint="If discovery fails, use the printer's IP address and check container routing and firewall rules."
                if advisory
                else None,
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                id="network_mode",
                status="skip",
                title="Container networking",
                params={"runtime": runtime, "reason": "unknown" if not mode_known or runtime else "not_container"},
                message="The container network mode could not be established."
                if runtime
                else "Container network-mode checks are not needed for this environment.",
            )
        )

    checks.append(
        DiagnosticCheck(
            id="route_source",
            status="pass" if route_known and source else "skip",
            title="Route to printer",
            params={"printer_ip": str(destination), **({"source_ip": source} if source else {})},
            message=f"The local route selects {source} as its source address. No packet was sent and printer reachability was not tested."
            if source
            else "The local route's source address could not be determined within the time limit.",
            hint=None if source else "If the printer cannot connect, check the host's routes and network interfaces.",
        )
    )
    network = _source_network(source, interfaces) if source and interfaces_known and interfaces else None
    if network:
        interface, subnet = network
        same = destination in ipaddress.ip_network(subnet)
        checks.append(
            DiagnosticCheck(
                id="subnet",
                status="pass" if same else "warn",
                title="Printer subnet",
                params={
                    "source_ip": source,
                    "printer_ip": str(destination),
                    "interface": interface,
                    "subnet": subnet,
                    "same_subnet": same,
                },
                message=f"The printer is inside the source interface's configured subnet ({subnet})."
                if same
                else f"The printer is outside the source interface's subnet ({subnet}). A routed connection can still work.",
                hint=None
                if same
                else "If a printer connection fails, check routing and firewall rules between these networks.",
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                id="subnet",
                status="skip",
                title="Printer subnet",
                params={"reason": "prefix_unavailable"},
                message="The selected source interface's prefix is unavailable, so the subnet relationship is unknown.",
            )
        )
    return checks
