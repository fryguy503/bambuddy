"""Shared network environment helpers and smart-plug discovery.

Legacy printer-discovery names re-export the selected Bambu plugin so existing
callers retain the same discovery sessions while new code uses the plugin API.
"""

import asyncio
import ipaddress
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.app.printers.bambu.discovery import (
        BAMBU_SEARCH_TARGET as BAMBU_SEARCH_TARGET,
        SSDP_ADDR as SSDP_ADDR,
        SSDP_MSEARCH as SSDP_MSEARCH,
        SSDP_PORT as SSDP_PORT,
        VIRTUAL_PRINTER_SERIAL_SUFFIX as VIRTUAL_PRINTER_SERIAL_SUFFIX,
        DiscoveredPrinter as DiscoveredPrinter,
        PrinterDiscoveryService as PrinterDiscoveryService,
        SubnetScanner as SubnetScanner,
        discovery_service as discovery_service,
        subnet_scanner as subnet_scanner,
    )

_PRINTER_DISCOVERY_EXPORTS = frozenset(
    {
        "BAMBU_SEARCH_TARGET",
        "SSDP_ADDR",
        "SSDP_MSEARCH",
        "SSDP_PORT",
        "VIRTUAL_PRINTER_SERIAL_SUFFIX",
        "DiscoveredPrinter",
        "PrinterDiscoveryService",
        "SubnetScanner",
        "discovery_service",
        "subnet_scanner",
    }
)


def __getattr__(name: str):
    if name not in _PRINTER_DISCOVERY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from backend.app.printers.compat import compatibility_module

    return getattr(compatibility_module("bambu", "discovery"), name)

logger = logging.getLogger(__name__)


# Runtime names :func:`detect_container_runtime` can return. These reach the
# user in the connection diagnostic, so they are the names people know their
# own setup by.
RUNTIME_DOCKER = "Docker"
RUNTIME_PODMAN = "Podman"
RUNTIME_KUBERNETES = "Kubernetes"
RUNTIME_CONTAINERD = "containerd"
RUNTIME_LXC = "LXC"
# Sentinel for "in a container we cannot name". The others are proper nouns
# that interpolate into any language; this one is localized by the frontend
# (diagnostic.check.network_mode.genericRuntime), so keep the two in step.
RUNTIME_OTHER = "container"

# Runtimes that put Bambuddy in an OCI container whose network mode is a
# choice the user made and can change. LXC is deliberately absent: a Proxmox
# or LXD system container is bridged onto the LAN like a small VM, so there is
# no "recreate it with host networking" advice to give.
OCI_RUNTIMES = frozenset({RUNTIME_DOCKER, RUNTIME_PODMAN, RUNTIME_KUBERNETES, RUNTIME_CONTAINERD, RUNTIME_OTHER})

# systemd writes the engine's own name here. It is the only signal that tells
# Podman apart from Docker without guessing, which is why it is consulted
# first (see updates.py, which has used it for the same reason for longer).
_SYSTEMD_CONTAINER = Path("/run/systemd/container")

_SYSTEMD_RUNTIME_NAMES = {
    "docker": RUNTIME_DOCKER,
    "podman": RUNTIME_PODMAN,
    "containerd": RUNTIME_CONTAINERD,
    "lxc": RUNTIME_LXC,
    "lxc-libvirt": RUNTIME_LXC,
    "oci": RUNTIME_OTHER,
}


def _read_text(path: Path) -> str:
    """Read a small /proc or /run marker file, empty string if unreadable."""
    try:
        return path.read_text()
    except (OSError, ValueError):
        # Unreadable, absent, or /proc entry that vanished mid-read.
        return ""


def detect_container_runtime() -> str | None:
    """Name the container runtime Bambuddy is running under, or None.

    ``is_running_in_docker`` below answers a narrower question and is
    deliberately left alone — see the comment on it.

    Detection is ordered most-specific first, because the generic markers
    cannot tell two engines apart: Podman sets ``/run/.containerenv`` *and*
    writes ``libpod`` into the cgroup path, while Docker sets ``/.dockerenv``
    and writes ``docker``. A container started by neither still gets a name
    (``container``) rather than None, because "we are in something" is a
    useful answer even when the engine is not.
    """
    systemd_name = _read_text(_SYSTEMD_CONTAINER).strip().lower()
    if systemd_name:
        return _SYSTEMD_RUNTIME_NAMES.get(systemd_name, RUNTIME_OTHER)

    # Podman writes /run/.containerenv into every container it starts. Older
    # versions put it at the root, so both are checked.
    if Path("/run/.containerenv").exists() or Path("/.containerenv").exists():
        return RUNTIME_PODMAN
    if Path("/.dockerenv").exists():
        return RUNTIME_DOCKER

    cgroup = _read_text(Path("/proc/1/cgroup"))
    if "libpod" in cgroup:
        return RUNTIME_PODMAN
    if "kubepods" in cgroup:
        return RUNTIME_KUBERNETES
    if "docker" in cgroup:
        return RUNTIME_DOCKER
    if "containerd" in cgroup:
        return RUNTIME_CONTAINERD
    if "/lxc" in cgroup:
        return RUNTIME_LXC

    env_name = (os.environ.get("CONTAINER") or "").strip().lower()
    if env_name:
        return _SYSTEMD_RUNTIME_NAMES.get(env_name, RUNTIME_OTHER)
    if os.environ.get("DOCKER_CONTAINER"):
        return RUNTIME_DOCKER

    return None


def is_running_in_docker() -> bool:
    """Detect if we're running inside a Docker container.

    Kept Docker-specific on purpose, and NOT rewritten on top of
    :func:`detect_container_runtime`. Three callers key real behaviour off
    this: ``/api/discovery/info`` feeds it to the Add-Printer flow, where
    ``isDocker`` switches discovery from SSDP to subnet scanning, and the
    backup-path probe and support bundle both read it. Answering True for a
    host-networked Podman container would take SSDP away from users for whom
    it works (#3092). Widening it is a separate decision from naming the
    runtime, so it is made separately.
    """
    # Check for .dockerenv file
    if Path("/.dockerenv").exists():
        return True

    # Check cgroup for docker/containerd
    try:
        with open("/proc/1/cgroup") as f:
            content = f.read()
            if "docker" in content or "containerd" in content or "kubepods" in content:
                return True
    except (FileNotFoundError, PermissionError):
        pass  # /proc/1/cgroup may not exist or be readable; fall through to env check

    # Check for container environment variable
    return bool(os.environ.get("CONTAINER") or os.environ.get("DOCKER_CONTAINER"))


class TasmotaScanner:
    """Scanner for discovering Tasmota devices by probing IP addresses."""

    HTTP_PORT = 80

    def __init__(self):
        self._discovered: dict[str, dict] = {}
        self._running = False
        self._scanned = 0
        self._total = 0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def discovered_devices(self) -> list[dict]:
        return list(self._discovered.values())

    @property
    def progress(self) -> tuple[int, int]:
        """Return (scanned, total) counts."""
        return self._scanned, self._total

    async def scan_range(self, from_ip: str, to_ip: str, timeout: float = 1.0) -> list[dict]:
        """Scan an IP range for Tasmota devices.

        Args:
            from_ip: Starting IP address (e.g., "192.168.1.1")
            to_ip: Ending IP address (e.g., "192.168.1.254")
            timeout: Connection timeout per host in seconds

        Returns:
            List of discovered Tasmota devices
        """
        if self._running:
            return []

        self._running = True
        self._discovered.clear()
        self._scanned = 0

        try:
            start = ipaddress.ip_address(from_ip)
            end = ipaddress.ip_address(to_ip)

            # Generate list of IPs in range
            hosts = []
            current = start
            while current <= end:
                hosts.append(str(current))
                current = ipaddress.ip_address(int(current) + 1)

            self._total = len(hosts)

            if self._total > 1024:
                logger.warning("IP range has %s hosts, limiting to 1024", self._total)
                self._total = 1024
                hosts = hosts[:1024]

            logger.info("Starting Tasmota scan from %s to %s (%s hosts)", from_ip, to_ip, self._total)

            # Scan in batches to avoid overwhelming the network
            batch_size = 50
            for i in range(0, len(hosts), batch_size):
                if not self._running:
                    logger.info("Tasmota scan stopped by user")
                    break

                batch = hosts[i : i + batch_size]
                tasks = [self._probe_host(ip) for ip in batch]
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except Exception as e:
                    logger.warning("Batch %s error: %s", i // batch_size, e)
                self._scanned = min(i + batch_size, len(hosts))

            logger.info("Tasmota scan complete. Found %s devices.", len(self._discovered))
            return self.discovered_devices

        except ValueError as e:
            logger.error("Invalid IP address format: %s", e)
            return []
        finally:
            self._running = False

    async def _probe_host(self, ip: str):
        """Probe a single host for Tasmota HTTP API."""
        try:
            # Hard timeout of 5 seconds max per host
            await asyncio.wait_for(self._do_probe(ip), timeout=5.0)
        except TimeoutError:
            pass  # Host did not respond in time; skip
        except Exception:
            pass  # Probe failed for this host; skip silently

    async def _do_probe(self, ip: str):
        """Actually probe the host."""
        import httpx

        try:
            # Reasonable timeouts for network scanning
            client_timeout = httpx.Timeout(3.0, connect=1.0)
            async with httpx.AsyncClient(timeout=client_timeout, follow_redirects=False) as client:
                # First try simple Power command - most reliable indicator of Tasmota
                power_url = f"http://{ip}/cm?cmnd=Power"
                try:
                    power_response = await client.get(power_url)
                    if power_response.status_code == 401:
                        # Device requires auth - still a Tasmota device!
                        logger.info("Discovered Tasmota at %s (requires auth - 401)", ip)
                        device = {
                            "ip_address": ip,
                            "name": f"Tasmota ({ip})",
                            "module": None,
                            "state": "UNKNOWN",
                            "discovered_at": datetime.now(timezone.utc).isoformat(),
                        }
                        self._discovered[ip] = device
                        return

                    if power_response.status_code != 200:
                        return

                    power_data = power_response.json()

                    # Check for Tasmota auth warning (returns 200 with WARNING)
                    if "WARNING" in power_data:
                        logger.info("Discovered Tasmota at %s (requires auth)", ip)
                        device = {
                            "ip_address": ip,
                            "name": f"Tasmota ({ip})",
                            "module": None,
                            "state": "UNKNOWN",
                            "discovered_at": datetime.now(timezone.utc).isoformat(),
                        }
                        self._discovered[ip] = device
                        return

                    # Check if response looks like Tasmota (has POWER or POWER1 key)
                    power_state = power_data.get("POWER") or power_data.get("POWER1")
                    if power_state is None:
                        return

                except Exception as e:
                    logger.debug("Error probing %s: %s", ip, e)
                    return

                # It's a Tasmota device! Now get more info
                device_name = f"Tasmota ({ip})"
                module = None

                # Try to get device name from Status 0
                try:
                    status_url = f"http://{ip}/cm?cmnd=Status%200"
                    status_response = await client.get(status_url)
                    if status_response.status_code == 200:
                        status_data = status_response.json()
                        if "Status" in status_data:
                            status = status_data["Status"]
                            device_name = status.get("DeviceName") or device_name
                            if not device_name or device_name == f"Tasmota ({ip})":
                                # Try FriendlyName
                                friendly = status.get("FriendlyName")
                                if friendly and isinstance(friendly, list) and friendly[0]:
                                    device_name = friendly[0]
                            module = status.get("Module")
                except Exception:
                    pass  # Status query is optional; proceed with defaults

                device = {
                    "ip_address": ip,
                    "name": device_name,
                    "module": module,
                    "state": power_state,
                    "discovered_at": datetime.now(timezone.utc).isoformat(),
                }

                self._discovered[ip] = device
                logger.info("Discovered Tasmota device: %s at %s", device_name, ip)

        except httpx.TimeoutException:
            pass  # Host unreachable or too slow; not a Tasmota device
        except httpx.ConnectError:
            pass  # Connection refused; no HTTP server on this host
        except Exception:
            pass  # Unexpected error probing host; skip silently

    def stop(self):
        """Stop the current scan."""
        self._running = False


# Global instances
tasmota_scanner = TasmotaScanner()
