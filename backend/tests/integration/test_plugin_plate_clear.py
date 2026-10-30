"""The shared plate button respects an optional provider's durable gate."""

import pytest

from backend.app.printers.sdk import OperationDescriptor, OperationReceipt
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_printer_devices_api import create_sdk_printer, sdk_plugin as sdk_plugin


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_provider_plate_acknowledgement_precedes_host_clear(async_client, sdk_plugin, monkeypatch, accepted):
    describe = sdk_plugin.describe_device
    invoke = sdk_plugin.invoke

    def device(config):
        result = describe(config)
        result.operations.append(
            OperationDescriptor(
                id="job.clear_plate",
                label="Confirm plate cleared",
                permission="printers:clear_plate",
                result_schema={"type": "boolean"},
            )
        )
        return result

    async def operation(config, connection, request):
        if request.operation_id != "job.clear_plate":
            return await invoke(config, connection, request)
        assert printer_manager.is_awaiting_plate_clear(config.printer_id)
        sdk_plugin.calls.append(request)
        return OperationReceipt(
            operation_id=request.operation_id, request_id=request.request_id, status="succeeded", result=accepted
        )

    monkeypatch.setattr(sdk_plugin, "describe_device", device)
    monkeypatch.setattr(sdk_plugin, "invoke", operation)
    printer_id = await create_sdk_printer(async_client)
    printer_manager.set_awaiting_plate_clear(printer_id, True)
    try:
        response = await async_client.post(f"/api/v1/printers/{printer_id}/clear-plate")
        assert response.status_code == (200 if accepted else 409), response.text
        assert printer_manager.is_awaiting_plate_clear(printer_id) is not accepted
        assert sdk_plugin.calls[-1].operation_id == "job.clear_plate"
    finally:
        printer_manager._awaiting_plate_clear.discard(printer_id)
