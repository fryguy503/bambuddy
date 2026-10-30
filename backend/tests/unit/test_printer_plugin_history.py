"""Included material history belongs to its provider and optional lifecycle."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from backend.app.models.ams_history import AMSSensorHistory
from backend.app.models.printer import Printer
from backend.app.printers.bambu.plugin import BambuPrinterPlugin
from backend.app.printers.contracts import PrinterPlugin
from backend.tests.unit.test_ams_humidity_index_guard_3140 import _run_one_pass


@pytest.mark.asyncio
async def test_material_history_worker_ignores_other_provider_payloads(db_session, test_engine):
    printer = Printer(
        name="Other provider", plugin_id="other_provider", serial_number="other:1", ip_address="", access_code=""
    )
    db_session.add(printer)
    await db_session.commit()
    service = await _run_one_pass(
        test_engine,
        {"id": 0, "humidity_raw": "90", "temp": "60", "tray_exist_bits": "1", "tray": [{"tray_type": "PLA"}]},
    )
    rows = (await db_session.execute(select(AMSSensorHistory))).scalars().all()
    assert rows == []
    service.on_ams_humidity_high.assert_not_awaited()
    service.on_ams_temperature_high.assert_not_awaited()


@pytest.mark.asyncio
async def test_optional_host_hooks_only_start_included_history_for_bambu():
    class OtherPlugin(PrinterPlugin):
        def create_connection(self, config, **callbacks):
            raise AssertionError("No device connection belongs to a service lifecycle test")

    host = SimpleNamespace(start_ams_history_recording=MagicMock(), stop_ams_history_recording=MagicMock())
    other = OtherPlugin()
    await other.start_host_services(host)
    await other.stop_host_services(host)
    host.start_ams_history_recording.assert_not_called()
    host.stop_ams_history_recording.assert_not_called()
    bambu = BambuPrinterPlugin()
    await bambu.start_host_services(host)
    await bambu.stop_host_services(host)
    host.start_ams_history_recording.assert_called_once_with()
    host.stop_ams_history_recording.assert_called_once_with()
