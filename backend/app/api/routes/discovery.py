"""
Printer discovery API endpoints.

Provides endpoints for discovering printers through their installed addon.
Supports both SSDP discovery (for native installs) and subnet scanning (for Docker).
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.permissions import Permission
from backend.app.core.tasks import spawn_background_task
from backend.app.models.user import User
from backend.app.printers.contracts import PrinterPluginError
from backend.app.printers.registry import get_plugin
from backend.app.services.discovery import is_running_in_docker
from backend.app.services.network_utils import get_network_interfaces

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/discovery", tags=["discovery"])


class DiscoveryStatus(BaseModel):
    """Discovery status response."""

    running: bool


class DiscoveryInfo(BaseModel):
    """Discovery environment info."""

    is_docker: bool
    ssdp_running: bool
    scan_running: bool
    subnets: list[str] = []


class SubnetScanRequest(BaseModel):
    """Request to scan a subnet."""

    subnet: str  # CIDR notation, e.g., "192.168.1.0/24"
    timeout: float = 1.0  # Connection timeout per host
    plugin_id: str | None = None


class SubnetScanStatus(BaseModel):
    """Subnet scan status response."""

    running: bool
    scanned: int
    total: int


class DiscoveredPrinterResponse(BaseModel):
    """Discovered printer response."""

    serial: str
    name: str
    ip_address: str
    model: str | None = None
    discovered_at: str | None = None
    plugin_id: str = "bambu"


def _discovery_backends(plugin_id: str):
    try:
        plugin = get_plugin(plugin_id)
        return plugin.discovery_operation("service"), plugin.discovery_operation("subnet_scanner")
    except PrinterPluginError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/info", response_model=DiscoveryInfo)
async def get_discovery_info(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.DISCOVERY_SCAN),
    plugin_id: str = "bambu",
):
    """Get discovery environment info (Docker detection, etc.)."""
    discovery_service, subnet_scanner = _discovery_backends(plugin_id)
    subnets = [iface["subnet"] for iface in get_network_interfaces()]
    return DiscoveryInfo(
        is_docker=is_running_in_docker(),
        ssdp_running=discovery_service.is_running,
        scan_running=subnet_scanner.is_running,
        subnets=subnets,
    )


@router.get("/status", response_model=DiscoveryStatus)
async def get_discovery_status(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.DISCOVERY_SCAN),
    plugin_id: str = "bambu",
):
    """Get the current SSDP discovery status."""
    discovery_service, subnet_scanner = _discovery_backends(plugin_id)
    return DiscoveryStatus(running=discovery_service.is_running)


@router.post("/start", response_model=DiscoveryStatus)
async def start_discovery(
    duration: float = 10.0,
    _: User | None = RequirePermissionIfAuthEnabled(Permission.DISCOVERY_SCAN),
    plugin_id: str = "bambu",
):
    """Start SSDP printer discovery.

    Args:
        duration: Discovery duration in seconds (default 10)
    """
    discovery_service, subnet_scanner = _discovery_backends(plugin_id)
    await discovery_service.start(duration=duration)
    return DiscoveryStatus(running=discovery_service.is_running)


@router.post("/stop", response_model=DiscoveryStatus)
async def stop_discovery(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.DISCOVERY_SCAN),
    plugin_id: str = "bambu",
):
    """Stop SSDP printer discovery."""
    discovery_service, subnet_scanner = _discovery_backends(plugin_id)
    await discovery_service.stop()
    return DiscoveryStatus(running=discovery_service.is_running)


@router.get("/printers", response_model=list[DiscoveredPrinterResponse])
async def get_discovered_printers(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.DISCOVERY_SCAN),
    plugin_id: str = "bambu",
):
    """Get list of discovered printers (from both SSDP and subnet scan)."""
    discovery_service, subnet_scanner = _discovery_backends(plugin_id)
    # Combine results from both discovery methods
    printers = {}

    # Add SSDP discovered printers
    for p in discovery_service.discovered_printers:
        printers[p.ip_address] = p

    # Add subnet scan discovered printers (may override if same IP)
    for p in subnet_scanner.discovered_printers:
        if p.ip_address not in printers:
            printers[p.ip_address] = p

    return [
        DiscoveredPrinterResponse(
            serial=p.serial,
            name=p.name,
            ip_address=p.ip_address,
            model=p.model,
            discovered_at=p.discovered_at,
            plugin_id=plugin_id,
        )
        for p in printers.values()
    ]


# Subnet scanning endpoints (for Docker environments)


@router.post("/scan", response_model=SubnetScanStatus)
async def start_subnet_scan(
    request: SubnetScanRequest,
    _: User | None = RequirePermissionIfAuthEnabled(Permission.DISCOVERY_SCAN),
    plugin_id: str = "bambu",
):
    """Start a subnet scan through the selected printer addon.

    Use this when running in Docker where SSDP multicast doesn't work.

    Args:
        request: Subnet to scan in CIDR notation (e.g., "192.168.1.0/24")
    """
    discovery_service, subnet_scanner = _discovery_backends(request.plugin_id or plugin_id)
    # Start scan in background
    spawn_background_task(
        subnet_scanner.scan_subnet(request.subnet, request.timeout),
        name=f"subnet-scan-{request.subnet}",
    )

    # Return immediate status
    scanned, total = subnet_scanner.progress
    return SubnetScanStatus(
        running=subnet_scanner.is_running,
        scanned=scanned,
        total=total,
    )


@router.get("/scan/status", response_model=SubnetScanStatus)
async def get_scan_status(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.DISCOVERY_SCAN),
    plugin_id: str = "bambu",
):
    """Get the current subnet scan status."""
    discovery_service, subnet_scanner = _discovery_backends(plugin_id)
    scanned, total = subnet_scanner.progress
    return SubnetScanStatus(
        running=subnet_scanner.is_running,
        scanned=scanned,
        total=total,
    )


@router.post("/scan/stop", response_model=SubnetScanStatus)
async def stop_subnet_scan(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.DISCOVERY_SCAN),
    plugin_id: str = "bambu",
):
    """Stop the current subnet scan."""
    discovery_service, subnet_scanner = _discovery_backends(plugin_id)
    subnet_scanner.stop()
    scanned, total = subnet_scanner.progress
    return SubnetScanStatus(
        running=subnet_scanner.is_running,
        scanned=scanned,
        total=total,
    )
