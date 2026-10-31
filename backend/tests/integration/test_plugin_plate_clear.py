"""The shared plate button respects an optional provider's durable gate."""

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.printers.sdk import OperationDescriptor, OperationReceipt
from backend.app.printers.sdk.host import HostEventBridge
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


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("retained_writer", [False, True])
async def test_provider_snapshot_ack_updates_cached_bridge_before_immediate_queue_start(
    async_client, sdk_plugin, db_session, archive_factory, monkeypatch, accepted, retained_writer
):
    describe = sdk_plugin.describe_device
    invoke = sdk_plugin.invoke

    def device(config):
        descriptor = describe(config)
        if retained_writer:
            descriptor.extensions["plate_clear_policy"] = "required"
        descriptor.operations.append(
            OperationDescriptor(
                id="job.clear_plate",
                label="Confirm plate cleared",
                permission="printers:clear_plate",
                result_schema={"type": "boolean"},
            )
        )
        return descriptor

    async def operation(config, connection, request):
        if request.operation_id != "job.clear_plate":
            return await invoke(config, connection, request)
        sdk_plugin.calls.append(request)
        if accepted:
            # This is the provider's successful durable-ack contract: publish
            # the new snapshot before returning, without waiting for telemetry.
            connection.state.extensions["plate_clear_required"] = False
            connection.publish()
        return OperationReceipt(
            operation_id=request.operation_id, request_id=request.request_id, status="succeeded", result=accepted
        )

    monkeypatch.setattr(sdk_plugin, "describe_device", device)
    monkeypatch.setattr(sdk_plugin, "invoke", operation)
    printer_id = await create_sdk_printer(async_client)
    raw = sdk_plugin.connections[-1]
    adapter = printer_manager.get_client(printer_id)
    # The lightweight API fixture omits manager lifecycle callbacks. Install
    # the real SDK bridge here to reproduce production's cached snapshot path.
    bridge = HostEventBridge(adapter._config, sdk_plugin, {})
    adapter._event_bridge = bridge
    raw.callbacks["on_event"] = bridge.emit
    raw.state.extensions["plate_clear_required"] = not retained_writer
    raw.publish()
    assert adapter._event_bridge.latest_snapshot.extensions["plate_clear_required"] is not retained_writer
    monkeypatch.setattr(printer_manager, "_awaiting_plate_clear", set())
    if retained_writer:
        # The old retained workflow finishes after the refreshed observer has
        # cached its clean snapshot and raises the existing host callback flag.
        printer_manager.set_awaiting_plate_clear(printer_id, True)
        assert adapter.state.plate_clear_required is False
        assert adapter.state.requires_plate_clear is True
    archive = await archive_factory(printer_id, with_run=False)
    item = PrintQueueItem(printer_id=printer_id, archive_id=archive.id, status="pending", manual_start=True)
    db_session.add(item)
    await db_session.commit()
    item_id = item.id

    status = await async_client.get(f"/api/v1/printers/{printer_id}/status")
    assert status.status_code == 200, status.text
    assert status.json()["plate_clear_required"] is True
    assert status.json()["awaiting_plate_clear"] is True
    first = await async_client.post(f"/api/v1/queue/{item_id}/start?skip_filament_check=true")
    assert first.status_code == 409
    clear = await async_client.post(f"/api/v1/printers/{printer_id}/clear-plate")
    assert clear.status_code == (200 if accepted else 409), clear.text
    retry = await async_client.post(f"/api/v1/queue/{item_id}/start?skip_filament_check=true")
    assert retry.status_code == (200 if accepted else 409), retry.text
    assert printer_manager.provider_requires_plate_clear(printer_id) is not accepted
    if accepted:
        assert adapter._event_bridge.latest_snapshot.extensions["plate_clear_required"] is False
    assert [request.operation_id for request in sdk_plugin.calls] == ["job.clear_plate"]
    await db_session.refresh(item)
    assert item.status == "pending" and item.manual_start is not accepted
