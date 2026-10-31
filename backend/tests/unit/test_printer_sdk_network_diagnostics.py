"""Read-only diagnostics use local route evidence and never probe a service."""

import asyncio
import ipaddress
import json
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.printers.sdk import network_diagnostics as diagnostics


@pytest.fixture
def probes():
    with (
        patch.object(diagnostics, "detect_container_runtime", return_value=None) as runtime,
        patch.object(diagnostics, "_route_source", return_value="192.168.96.9") as route,
        patch.object(
            diagnostics,
            "get_network_interfaces",
            return_value=[
                {
                    "name": "lan0",
                    "ip": "192.168.96.9",
                    "netmask": "255.255.252.0",
                    "subnet": "192.168.96.0/22",
                }
            ],
        ) as interfaces,
        patch.object(diagnostics, "_network_mode", return_value=None) as mode,
    ):
        yield SimpleNamespace(runtime=runtime, route=route, interfaces=interfaces, mode=mode)


def by_id(checks):
    return {check.id: check for check in checks}


async def test_native_host_uses_real_interface_prefix_and_no_discovery(probes):
    checks = by_id(await diagnostics.network_diagnostic_checks("192.168.99.70"))
    assert checks["environment"].status == "pass"
    assert checks["environment"].params["container"] is False
    assert checks["network_mode"].status == "skip"
    assert checks["route_source"].params["source_ip"] == "192.168.96.9"
    assert checks["subnet"].status == "pass"
    assert checks["subnet"].params["subnet"] == "192.168.96.0/22"
    probes.interfaces.assert_called_once_with(include_excluded=True)
    probes.route.assert_called_once_with(ipaddress.ip_address("192.168.99.70"))
    probes.mode.assert_not_called()
    assert all(check.title and check.message for check in checks.values())


async def test_docker_bridge_is_advisory_even_with_routed_printer(probes):
    probes.runtime.return_value = "Docker"
    probes.mode.return_value = "bridge"
    probes.route.return_value = "172.18.0.2"
    probes.interfaces.return_value = [{"name": "eth0", "ip": "172.18.0.2", "netmask": "255.255.0.0"}]
    checks = by_id(await diagnostics.network_diagnostic_checks("192.168.99.70"))
    assert checks["network_mode"].status == "warn"
    assert checks["network_mode"].params["mode"] == "bridge"
    assert "direct printer connections can still work" in checks["network_mode"].message
    assert checks["subnet"].status == "warn"
    assert "routed connection can still work" in checks["subnet"].message
    assert all(check.status != "fail" for check in checks.values())


@pytest.mark.parametrize("runtime, mode", [("Docker", "host"), ("Podman", "macvlan"), ("containerd", "ipvlan")])
async def test_container_with_host_or_direct_lan_interfaces(probes, runtime, mode):
    probes.runtime.return_value = runtime
    probes.mode.return_value = mode
    checks = by_id(await diagnostics.network_diagnostic_checks("192.168.99.70"))
    assert checks["network_mode"].status == "pass"
    assert checks["environment"].params["runtime"] == runtime
    assert checks["subnet"].status == "pass"


async def test_routed_native_printer_is_not_a_failed_connection(probes):
    checks = by_id(await diagnostics.network_diagnostic_checks("10.40.0.5"))
    assert checks["route_source"].status == "pass"
    assert checks["subnet"].status == "warn"
    assert checks["subnet"].params["same_subnet"] is False
    assert all(check.status != "fail" for check in checks.values())


@pytest.mark.parametrize(
    "interfaces",
    [
        [],
        [{"name": "lan0", "ip": "192.168.96.9"}],
        [{"name": "lan0", "ip": "192.168.96.9", "subnet": "not-a-network"}],
        [{"name": "other0", "ip": "192.168.96.10", "subnet": "192.168.96.0/22"}],
    ],
)
async def test_missing_or_malformed_source_prefix_is_unknown_not_guessed(probes, interfaces):
    probes.interfaces.return_value = interfaces
    checks = by_id(await diagnostics.network_diagnostic_checks("192.168.99.70"))
    assert checks["subnet"].status == "skip"
    assert "same_subnet" not in checks["subnet"].params
    assert "/24" not in checks["subnet"].message


async def test_unavailable_platform_apis_degrade_independently(probes):
    probes.runtime.side_effect = OSError("unreadable markers")
    probes.route.side_effect = OSError("no route")
    probes.interfaces.side_effect = OSError("unavailable interfaces")
    checks = by_id(await diagnostics.network_diagnostic_checks("192.168.99.70"))
    assert all(check.status == "skip" for check in checks.values())


@pytest.mark.parametrize("address", ["", "printer.local", "192.168.1.999", "not/an/address"])
async def test_malformed_or_hostname_address_never_resolves_or_uses_os_probes(probes, address):
    checks = await diagnostics.network_diagnostic_checks(address)
    assert len(checks) == 1
    assert checks[0].status == "skip"
    probes.runtime.assert_not_called()
    probes.route.assert_not_called()
    probes.interfaces.assert_not_called()


async def test_ipv6_route_does_not_claim_ipv4_subnet_relationship(probes):
    probes.route.return_value = "fd00::2"
    checks = by_id(await diagnostics.network_diagnostic_checks("fd00::3"))
    assert checks["route_source"].params["source_ip"] == "fd00::2"
    assert checks["subnet"].status == "skip"


@pytest.mark.parametrize("address,family", [("192.168.99.70", socket.AF_INET), ("fd00::3", socket.AF_INET6)])
def test_route_lookup_connects_only_to_supplied_literal_without_sending(address, family):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.getsockname.return_value = ("192.168.96.9" if family == socket.AF_INET else "fd00::2", 1234)
    with patch.object(diagnostics.socket, "socket", return_value=connection) as factory:
        diagnostics._route_source(ipaddress.ip_address(address))
    factory.assert_called_once_with(family, socket.SOCK_DGRAM)
    connection.settimeout.assert_called_once_with(diagnostics._SOCKET_TIMEOUT_SECONDS)
    connection.connect.assert_called_once_with((address, 1))
    connection.send.assert_not_called()
    connection.sendto.assert_not_called()
    connection.sendall.assert_not_called()
    connection.__exit__.assert_called_once()


@pytest.mark.parametrize(
    "links,native,expected",
    [
        ([{"ifname": "eth0", "linkinfo": {"info_kind": "veth"}}], False, "bridge"),
        ([{"ifname": "eth0"}], True, "host"),
        ([{"ifname": "eth0", "linkinfo": {"info_kind": "macvlan"}}], False, "macvlan"),
        ([{"ifname": "eth0", "linkinfo": {"info_kind": "ipvlan", "info_data": {"mode": "l3"}}}], False, "ipvlan_l3"),
        ([{"ifname": "eth0", "linkinfo": {"info_kind": "veth"}}, {"ifname": "mystery0"}], False, None),
    ],
)
def test_linux_network_mode_uses_kernel_evidence_with_bounded_command(links, native, expected):
    with (
        patch.object(diagnostics.sys, "platform", "linux"),
        patch.object(diagnostics, "_native_interface_present", return_value=native),
        patch.object(
            diagnostics.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(links))
        ) as command,
    ):
        assert diagnostics._network_mode() == expected
    command.assert_called_once_with(
        ["ip", "-d", "-j", "link", "show"], capture_output=True, text=True, timeout=diagnostics._COMMAND_TIMEOUT_SECONDS
    )


def test_missing_or_timed_out_ip_command_does_not_guess_bridge():
    with (
        patch.object(diagnostics.sys, "platform", "linux"),
        patch.object(diagnostics, "_native_interface_present", return_value=False),
        patch.object(diagnostics.subprocess, "run", side_effect=subprocess.TimeoutExpired("ip", 0.5)),
    ):
        assert diagnostics._network_mode() is None


async def test_blocking_probes_are_offloaded_bounded_and_do_not_stall_event_loop(probes):
    release = threading.Event()
    main_thread = threading.get_ident()
    threads = []

    def blocked(*args, **kwargs):
        threads.append(threading.get_ident())
        release.wait(timeout=1)
        return None

    probes.runtime.side_effect = blocked
    probes.route.side_effect = blocked
    probes.interfaces.side_effect = blocked
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.005)
            ticks += 1

    try:
        with patch.object(diagnostics, "_PROBE_TIMEOUT_SECONDS", 0.03):
            started = time.monotonic()
            checks, _ = await asyncio.gather(diagnostics.network_diagnostic_checks("192.168.99.70"), heartbeat())
            assert time.monotonic() - started < 0.3
        assert ticks == 5
        assert len(threads) == 3 and all(thread != main_thread for thread in threads)
        assert all(check.status == "skip" for check in checks)
    finally:
        release.set()
