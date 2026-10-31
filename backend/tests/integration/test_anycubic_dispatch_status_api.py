"""Detailed pending-dispatch metadata requires control permission and printer scope."""

from dataclasses import replace

import pytest

from backend.app.models.printer import Printer
from backend.app.printers import api, registry as registry_module
from backend.app.printers.api import printer_api
from backend.app.printers.contracts import PrinterConfig
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_printer_scope_1727 import _admin_token, _auth, _group, _user
from plugins.anycubic.payload.plugin import AnycubicPrinterPlugin

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def managed(db_session, tmp_path, monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "printer_plugin_dir", tmp_path / "installed")
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    registry = registry_module.PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", registry)
    registry.load()
    plugin = AnycubicPrinterPlugin()
    registry.register(plugin)
    monkeypatch.setattr(api, "_bindings", {})
    monkeypatch.setattr(printer_manager, "_clients", {})
    monkeypatch.setattr(printer_manager, "_configs", {})
    printers = []
    for number in (1, 2):
        printer = Printer(
            name=f"Status fixture {number}",
            ip_address=f"192.168.50.{number}",
            serial_number=f"STATUS-API-{number}",
            access_code="",
            model="Kobra S1",
            plugin_id="anycubic",
            plugin_options={"tls_fingerprint": "a" * 64},
        )
        db_session.add(printer)
        await db_session.flush()
        config = PrinterConfig.from_printer(printer)
        adapter = printer_api.create_connection(printer)
        printer_manager._clients[printer.id] = adapter
        printer_manager._configs[printer.id] = config
        adapter._connection.journal.begin(
            f"queue:held-{number}",
            {
                "status": "unknown",
                "native_filename": "/private/data/confirmed-123456789abc.gcode",
                "token": "PRIVATE-TOKEN",
                "path": "/private/data/secret",
                "bindings": {"password": "PRIVATE-PASSWORD"},
            },
        )
        printers.append((printer, adapter))
    await db_session.commit()
    return printers


async def test_control_and_scope_protect_details_without_clearing_holds(async_client, managed):
    first, second = managed
    admin = await _admin_token(async_client)
    group = await _group(
        async_client, admin, "dispatch_operators", permissions=["printers:control"], printer_ids=[first[0].id]
    )
    operator, _ = await _user(async_client, admin, "dispatch_operator", [group])
    reader_group = await _group(
        async_client, admin, "dispatch_readers", permissions=["printers:read"], printer_ids=[first[0].id]
    )
    reader, _ = await _user(async_client, admin, "dispatch_reader", [reader_group])
    path = "/api/v1/printer-plugins/anycubic/printers/{}/dispatch-holds"
    response = await async_client.get(path.format(first[0].id), headers=_auth(operator))
    assert response.status_code == 200, response.text
    hold = response.json()["holds"][0]
    assert set(hold) == {"job_id", "status", "native_filename", "created_at"}
    assert hold["native_filename"] == "confirmed-123456789abc.gcode"
    assert "PRIVATE" not in response.text and "/private" not in response.text
    assert (await async_client.get(path.format(second[0].id), headers=_auth(operator))).status_code == 404
    assert (await async_client.get(path.format(first[0].id), headers=_auth(reader))).status_code == 403
    assert first[1]._connection.journal.get("queue:held-1")["status"] == "unknown"


async def test_changed_managed_configuration_is_rejected(async_client, managed):
    printer, adapter = managed[0]
    adapter._config = replace(adapter._config, options={"tls_fingerprint": "b" * 64})
    response = await async_client.get(f"/api/v1/printer-plugins/anycubic/printers/{printer.id}/dispatch-holds")
    assert response.status_code == 409
