"""Bounded, read-only discovery of stock Anycubic LAN printers over HTTP."""

import asyncio
import contextlib
import ipaddress
import math
from datetime import datetime, timezone

import aiohttp

from backend.app.core.tasks import spawn_background_task
from backend.app.services.discovery import DiscoveredPrinter

from .protocol import MAX_BYTES, AnycubicError, identity, json_object, model_from_info, validate_info

MAX_ADDRESSES = 1024
CONCURRENCY = 32
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def scan_network(subnet: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(subnet, strict=False)
    except (ValueError, TypeError):
        raise AnycubicError("Enter a private IPv4 subnet in CIDR notation") from None
    if (
        not isinstance(network, ipaddress.IPv4Network)
        or network.num_addresses > MAX_ADDRESSES
        or not any(network.subnet_of(allowed) for allowed in PRIVATE_NETWORKS)
    ):
        raise AnycubicError("Choose a private IPv4 subnet with at most 1024 addresses (/22 or smaller)")
    return network


class AnycubicSubnetScanner:
    """One cancellable scan; results contain only public identity metadata.

    This reads GET /info on the fixed LAN port. It does not pair, inspect or
    trust certificates, obtain MQTT credentials, or send printer commands.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._task = None
        self._running = False
        self._discovered = {}
        self._scanned = self._total = 0

    @property
    def is_running(self):
        return self._running

    @property
    def progress(self):
        return self._scanned, self._total

    @property
    def discovered_printers(self):
        return [self._discovered[ip] for ip in sorted(self._discovered, key=ipaddress.ip_address)]

    async def start(self, subnet: str, timeout: float = 1.0):
        network = scan_network(subnet)
        if not math.isfinite(timeout) or not 0.1 <= timeout <= 3:
            raise AnycubicError("Scan timeout must be between 0.1 and 3 seconds")
        async with self._lock:
            if self._running:
                raise RuntimeError("An Anycubic LAN scan is already running")
            hosts = [str(host) for host in network.hosts()]
            self._discovered = {}
            self._scanned, self._total = 0, len(hosts)
            self._running = True
            self._task = spawn_background_task(self._scan(hosts, timeout), name="anycubic-lan-scan")

    async def stop(self):
        async with self._lock:
            task = self._task
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                self._task = None
            self._running = False

    async def _scan(self, hosts, timeout):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
                connector=aiohttp.TCPConnector(limit=CONCURRENCY),
                trust_env=False,
                auto_decompress=False,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            ) as session:
                for offset in range(0, len(hosts), CONCURRENCY):
                    batch = hosts[offset : offset + CONCURRENCY]
                    await asyncio.gather(*(self._probe(session, host) for host in batch))
                    self._scanned += len(batch)
        finally:
            self._running = False

    async def _probe(self, session, host):
        try:
            async with session.get(f"http://{host}:18910/info", allow_redirects=False) as response:
                if response.status != 200:
                    return
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdecimal() or len(length) > 10 or int(length) > MAX_BYTES):
                    return
                payload = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    if len(payload) + len(chunk) > MAX_BYTES:
                        return
                    payload.extend(chunk)
            info = validate_info(host, json_object(bytes(payload)))
            model = model_from_info(info)
            self._discovered[host] = DiscoveredPrinter(
                serial=identity(info),
                name=f"Anycubic {model.name}",
                ip_address=host,
                model=model.name,
                discovered_at=datetime.now(timezone.utc).isoformat(),
            )
        except (aiohttp.ClientError, TimeoutError, AnycubicError, ValueError, TypeError, RecursionError):
            pass  # Unrelated hosts, non-LAN printers and malformed replies are not results.


anycubic_scanner = AnycubicSubnetScanner()
