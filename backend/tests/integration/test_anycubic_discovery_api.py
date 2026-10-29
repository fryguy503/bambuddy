"""Provider-specific discovery results, admission and lifecycle, without LAN I/O."""

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.api.routes import discovery as routes
from backend.app.core.auth import generate_api_key
from backend.app.models.api_key import APIKey
from backend.app.models.settings import Settings
from backend.app.services.anycubic.discovery import AnycubicSubnetScanner
from backend.app.services.discovery import DiscoveredPrinter

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
ROOT = "/api/v1/discovery"


@pytest.fixture
async def scanner(monkeypatch):
    scanner = AnycubicSubnetScanner()
    monkeypatch.setattr(routes, "anycubic_scanner", scanner)
    yield scanner
    await scanner.stop()


async def test_anycubic_results_are_separate_from_bambu_results(async_client, scanner):
    scanner._discovered["192.168.1.151"] = DiscoveredPrinter(
        serial="s1-test",
        name="Anycubic Kobra S1",
        ip_address="192.168.1.151",
        model="Kobra S1",
    )
    with (
        patch.object(routes.subnet_scanner, "_discovered", {}),
        patch.object(routes.discovery_service, "_discovered", {}),
    ):
        assert (await async_client.get(ROOT + "/printers")).json() == []
        response = await async_client.get(ROOT + "/printers?printer_type=anycubic")
        assert response.status_code == 200
        assert response.json()[0]["serial"] == "s1-test"
        assert "token" not in response.text
        assert "tls_fingerprint" not in response.text


async def test_detected_subnets_use_os_masks_include_aliases_and_remove_duplicates(async_client):
    interfaces = [
        {"subnet": "10.42.8.0/23", "ip": "10.42.9.7", "is_alias": False},
        {"subnet": "172.22.4.0/22", "ip": "172.22.5.20", "is_alias": True},
        {"subnet": "10.42.8.0/23", "ip": "10.42.9.8", "is_alias": True},
    ]
    with patch.object(routes, "get_all_interface_ips", return_value=interfaces):
        response = await async_client.get(ROOT + "/info")
    assert response.status_code == 200
    assert response.json()["subnets"] == ["10.42.8.0/23", "172.22.4.0/22"]


async def test_start_reports_running_before_returning_and_stop_cancels_only_anycubic(async_client, scanner):
    import asyncio

    entered = asyncio.Event()

    async def blocked_scan(_hosts, _timeout):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            scanner._running = False

    with (
        patch.object(scanner, "_scan", side_effect=blocked_scan),
        patch.object(routes.subnet_scanner, "stop") as bambu_stop,
    ):
        response = await async_client.post(
            ROOT + "/scan",
            json={
                "subnet": "192.168.1.0/24",
                "printer_type": "anycubic",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"running": True, "scanned": 0, "total": 254}
        await asyncio.wait_for(entered.wait(), 1)
        assert (await async_client.get(ROOT + "/scan/status?printer_type=anycubic")).json()["running"] is True
        assert (
            await async_client.post(
                ROOT + "/scan",
                json={
                    "subnet": "192.168.1.0/24",
                    "printer_type": "anycubic",
                },
            )
        ).status_code == 409
        response = await async_client.post(ROOT + "/scan/stop?printer_type=anycubic")
        assert response.status_code == 200
        assert response.json()["running"] is False
        bambu_stop.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"subnet": "0.0.0.0/0"},
        {"subnet": "192.168.0.0/16"},
        {"subnet": "8.8.8.0/24"},
        {"subnet": "::1/128"},
        {"timeout": 0},
        {"timeout": 5},
        {"printer_type": "other"},
    ],
)
async def test_invalid_scan_request_is_rejected_without_network(async_client, scanner, changes):
    with patch.object(scanner, "_scan", new_callable=AsyncMock) as network:
        response = await async_client.post(
            ROOT + "/scan",
            json={
                "subnet": "192.168.1.0/24",
                "timeout": 1,
                "printer_type": "anycubic",
                **changes,
            },
        )
        assert response.status_code == 422
        network.assert_not_called()
        assert not scanner.is_running


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/scan", {"subnet": "192.168.1.0/24", "printer_type": "anycubic"}),
        ("GET", "/printers?printer_type=anycubic", None),
        ("GET", "/scan/status?printer_type=anycubic", None),
        ("POST", "/scan/stop?printer_type=anycubic", None),
    ],
)
async def test_anycubic_discovery_retains_api_key_administrative_fence(
    async_client,
    db_session,
    scanner,
    method,
    path,
    body,
):
    full_key, key_hash, key_prefix = generate_api_key()
    db_session.add(APIKey(name="discovery-test", key_hash=key_hash, key_prefix=key_prefix, enabled=True))
    db_session.add(Settings(key="auth_enabled", value="true"))
    await db_session.commit()
    with patch.object(scanner, "_scan", new_callable=AsyncMock) as network:
        response = await async_client.request(
            method,
            ROOT + path,
            json=body,
            headers={"X-API-Key": full_key},
        )
        assert response.status_code == 403
        assert "administrative operations" in response.json()["detail"]
        network.assert_not_called()
