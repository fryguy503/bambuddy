"""Plugin-owned diagnostics preserve core authorization and saved configuration."""

import pytest

from backend.app.printers.sdk import ConnectionDiagnosticResult, DiagnosticCheck
from backend.tests.integration.test_printer_devices_api import SixToolPlugin, create_sdk_printer
from backend.tests.integration.test_printer_scope_1727 import _admin_token, _auth, _group, _user

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def diagnostic_plugin(monkeypatch):
    from backend.app.printers import api, registry as registry_module
    from backend.app.services.printer_manager import printer_manager

    class DescribingPlugin(SixToolPlugin):
        async def connection_diagnostics(self, config, *, connection=None, wait_for_publish_seconds=0, progress=None):
            self.diagnostic_config = config
            self.diagnostic_connection = connection
            self.diagnostic_runs = getattr(self, "diagnostic_runs", 0) + 1
            return ConnectionDiagnosticResult(
                printer_id=config.printer_id,
                ip_address=config.ip_address,
                overall="warnings",
                checks=[
                    DiagnosticCheck(
                        id="thirdparty:toolchanger",
                        status="warn",
                        title="Toolchanger",
                        message="An accessory has no fresh status",
                        hint="Check its connection",
                    )
                ],
            )

    registry = registry_module.PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", registry)
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    registry.load()
    plugin = DescribingPlugin()
    registry.register(plugin)
    monkeypatch.setattr(api, "_bindings", {})
    for name in ("_clients", "_configs", "_models", "_printer_info", "_last_trays"):
        monkeypatch.setattr(printer_manager, name, {})
    return plugin


async def test_unsaved_model_options_reach_plugin_without_becoming_response_fields(async_client, diagnostic_plugin):
    response = await async_client.post(
        "/api/v1/printers/diagnostic",
        json={
            "plugin_id": diagnostic_plugin.id,
            "ip_address": "192.168.50.42",
            "model": "simulator",
            "serial_number": "USER-SELECTED",
            "access_code": "PRIVATE-ACCESS",
            "plugin_options": {"token": "PRIVATE-TOKEN", "tool_count": 6},
        },
    )
    assert response.status_code == 200, response.text
    config = diagnostic_plugin.diagnostic_config
    assert config.model == "simulator" and config.options["token"] == "PRIVATE-TOKEN"
    assert config.serial_number == "USER-SELECTED" and diagnostic_plugin.diagnostic_connection is None
    assert response.json()["checks"][0]["title"] == "Toolchanger"
    assert "PRIVATE-TOKEN" not in response.text and "PRIVATE-ACCESS" not in response.text
    assert diagnostic_plugin.calls == []


async def test_presave_diagnostic_requires_create_permission_before_probing(async_client, diagnostic_plugin):
    admin = await _admin_token(async_client)
    group = await _group(async_client, admin, "diagnostic_viewers", permissions=["printers:read"])
    token, _ = await _user(async_client, admin, "diagnostic_viewer", [group])
    response = await async_client.post(
        "/api/v1/printers/diagnostic",
        headers=_auth(token),
        json={
            "plugin_id": diagnostic_plugin.id,
            "ip_address": "192.168.50.42",
            "model": "simulator",
        },
    )
    assert response.status_code == 403
    assert not hasattr(diagnostic_plugin, "diagnostic_runs")


async def test_saved_diagnostic_uses_persisted_options_even_for_non_network_provider(async_client, diagnostic_plugin):
    printer_id = await create_sdk_printer(async_client)
    response = await async_client.get(f"/api/v1/printers/{printer_id}/diagnostic")
    assert response.status_code == 200, response.text
    assert diagnostic_plugin.diagnostic_config.options == {"token": "CONFIG-SECRET", "tool_count": 6}
    assert response.json()["printer_id"] == printer_id and response.json()["ip_address"] == ""
    assert "CONFIG-SECRET" not in response.text and diagnostic_plugin.calls == []


async def test_saved_diagnostic_requires_read_permission_before_probing(async_client, diagnostic_plugin):
    admin = await _admin_token(async_client)
    printer_id = await create_sdk_printer(async_client, _auth(admin))
    group = await _group(async_client, admin, "diagnostic_no_printer_access", permissions=["archives:read_all"])
    token, _ = await _user(async_client, admin, "diagnostic_unprivileged", [group])
    response = await async_client.get(f"/api/v1/printers/{printer_id}/diagnostic", headers=_auth(token))
    assert response.status_code == 403
    assert not hasattr(diagnostic_plugin, "diagnostic_runs")
