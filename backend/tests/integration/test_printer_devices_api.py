"""API2 proves arbitrary resources without a Bambu-shaped driver state."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.printers import api as printer_api_module
from backend.app.printers.sdk import (
    JobArtifact,
    JobRequest,
    OperationDescriptor,
    OperationReceipt,
    ResourceDescriptor,
    ResourceProperty,
    TelemetryValue,
)
from backend.tests.integration.test_printer_scope_1727 import _admin_token, _auth, _group, _user
from examples.printer_plugins.reference.payload.plugin import ReferencePlugin

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class SixToolPlugin(ReferencePlugin):
    __test__ = False
    id = "test_six_tools"
    publisher_id = "test.sdk"
    configuration_schema = {
        "type": "object",
        "properties": {
            "token": {"type": "string", "minLength": 1, "writeOnly": True},
            "tool_count": {"type": "integer", "minimum": 6, "maximum": 6},
        },
        "required": ["token"],
        "additionalProperties": False,
    }

    def __init__(self):
        self.calls = []
        self.connections = []
        self.tasks = {}

    def describe_device(self, config):
        descriptor = super().describe_device(config)
        for resource in descriptor.resources:
            if resource.id == "tool:5":
                resource.id = "tool:violet"
            for relation, values in resource.relations.items():
                resource.relations[relation] = ["tool:violet" if value == "tool:5" else value for value in values]
        descriptor.resource("heater:5").properties["target"].value_schema["maximum"] = 520
        descriptor.resources.append(
            ResourceDescriptor(
                id="sensor:private",
                kind="sensor",
                label="Protected sensor",
                properties={
                    "value": ResourceProperty(value_schema={"type": "string"}, read_permission="printers:update"),
                },
            )
        )
        descriptor.operations.extend(
            [
                OperationDescriptor(
                    id="tool.select", label="Select tool", resource_kinds=["tool"], result_schema={"type": "boolean"}
                ),
                OperationDescriptor(
                    id="test.invalid_result", label="Invalid result fixture", result_schema={"type": "boolean"}
                ),
                OperationDescriptor(
                    id="task.calibrate",
                    label="Calibrate fixture",
                    asynchronous=True,
                    cancellable=True,
                    result_schema={"type": "boolean"},
                ),
                OperationDescriptor(
                    id="test.dual_scope",
                    label="Both permissions",
                    additional_permissions=["printers:update"],
                    result_schema={"type": "boolean"},
                ),
            ]
        )
        return descriptor

    def create_connection(self, config, **callbacks):
        connection = super().create_connection(config, **callbacks)
        next(state for state in connection.state.resources if state.resource_id == "sensor:private").properties[
            "value"
        ] = TelemetryValue(value="PRIVATE-TELEMETRY")
        self.connections.append(connection)
        return connection

    def operation_handlers(self, connection):
        original = super().operation_handlers(connection)

        def record(handler):
            def wrapped(request):
                self.calls.append(request)
                return handler(request)

            return wrapped

        def select_tool(request):
            connection.selected_tool = request.resource_id
            return True

        def calibrate(request):
            receipt = OperationReceipt(
                operation_id=request.operation_id,
                request_id=request.request_id,
                status="running",
                task_id="provider:" + request.request_id,
            )
            self.tasks[receipt.task_id] = receipt
            return receipt

        original.update(
            {
                "tool.select": select_tool,
                "test.invalid_result": lambda request: "invalid-boolean-result",
                "task.calibrate": calibrate,
                "test.dual_scope": lambda request: True,
            }
        )
        return {name: record(handler) for name, handler in original.items()}

    async def operation_status(self, config, connection, task_id):
        return self.tasks[task_id]

    async def cancel_operation(self, config, connection, task_id):
        receipt = self.tasks[task_id].model_copy(
            update={"status": "cancelled", "updated_at": datetime.now(timezone.utc)}
        )
        self.tasks[task_id] = receipt
        return receipt


@pytest.fixture
def sdk_plugin(monkeypatch):
    from backend.app.printers import registry as registry_module
    from backend.app.services.printer_manager import printer_manager

    registry = registry_module.PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    monkeypatch.setattr(registry_module, "registry", registry)
    registry.load()
    plugin = SixToolPlugin()
    registry.register(plugin)
    monkeypatch.setattr(printer_api_module, "_bindings", {})
    for name in ("_clients", "_configs", "_models", "_printer_info", "_last_trays"):
        monkeypatch.setattr(printer_manager, name, {})
    return plugin


async def create_sdk_printer(client, headers=None):
    response = await client.post(
        "/api/v1/printers/",
        headers=headers or {},
        json={
            "plugin_id": "test_six_tools",
            "name": "Six tool USB fixture",
            "model": "simulator",
            "plugin_options": {"token": "CONFIG-SECRET", "tool_count": 6},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["ip_address"] == ""
    assert response.json()["serial_number"].startswith("test_six_tools:")
    assert "CONFIG-SECRET" not in response.text
    return response.json()["id"]


async def test_observer_refresh_during_permission_await_keeps_command_on_original_provider(
    async_client, sdk_plugin, db_session, monkeypatch
):
    from backend.app.api.routes import printer_devices
    from backend.app.models.printer import Printer
    from backend.app.models.printer_operation import PrinterOperation
    from backend.app.printers.registry import RuntimeProvider, current_registry
    from backend.app.services.printer_manager import printer_manager

    printer_id = await create_sdk_printer(async_client)
    printer = await db_session.get(Printer, printer_id)
    original = printer_manager.get_client(printer_id)
    updated = SixToolPlugin()
    updated.version = "1.1.0"
    registry = current_registry()
    candidate = RuntimeProvider(updated, updated.version, {"version": updated.version, "source": "managed-package"})
    monkeypatch.setattr(printer_manager, "_retained_clients", {})
    switched = False

    async def permission_with_refresh(request, target_id, permission):
        nonlocal switched
        if not switched:
            switched = True
            registry.activate(candidate)
            prepared = printer_manager.prepare_connection(printer, updated)
            printer_manager.publish_connection(prepared, retain_old=True)

    monkeypatch.setattr(printer_devices, "_permission", permission_with_refresh)
    response = await async_client.post(
        f"/api/v1/printers/{printer_id}/operations/tool.select",
        json={"resource_id": "tool:violet", "request_id": "refresh-during-permission", "parameters": {}},
    )
    assert response.status_code == 200, response.text
    assert switched and len(sdk_plugin.calls) == 1 and updated.calls == []
    assert printer_manager.get_client(printer_id)._plugin is updated
    assert printer_manager.retained_connection(printer_id, sdk_plugin) is original
    row = await db_session.get(PrinterOperation, response.json()["task_id"])
    assert row.operation_schema["extensions"]["bambuddy:provider"]["version"] == sdk_plugin.version


@pytest.mark.parametrize("endpoint", ["invoke", "snapshot"])
async def test_observer_refresh_before_connection_lookup_keeps_original_provider_and_transport(
    async_client, sdk_plugin, db_session, monkeypatch, endpoint
):
    from backend.app.api.routes import printer_devices
    from backend.app.models.printer_operation import PrinterOperation
    from backend.app.printers.registry import RuntimeProvider, current_registry
    from backend.app.services.printer_manager import printer_manager

    printer_id = await create_sdk_printer(async_client)
    original = printer_manager.get_client(printer_id)
    updated = SixToolPlugin()
    updated.version = "1.1.0"
    registry = current_registry()
    candidate = RuntimeProvider(updated, updated.version, {"version": updated.version, "source": "managed-package"})
    monkeypatch.setattr(printer_manager, "_retained_clients", {})
    original_lookup = printer_devices._printer
    original_invoke = sdk_plugin.invoke
    original_snapshot = sdk_plugin.snapshot_device
    invoked_connections = []
    snapshot_connections = []
    switched = False

    async def lookup_with_refresh(db, target_id, *, lock=False):
        nonlocal switched
        printer = await original_lookup(db, target_id, lock=lock)
        if not switched:
            switched = True
            registry.activate(candidate)
            prepared = printer_manager.prepare_connection(printer, updated)
            printer_manager.publish_connection(prepared, retain_old=True)
        return printer

    async def record_invoke(config, connection, request):
        invoked_connections.append(connection)
        return await original_invoke(config, connection, request)

    def record_snapshot(config, connection):
        snapshot_connections.append(connection)
        return original_snapshot(config, connection)

    monkeypatch.setattr(printer_devices, "_printer", lookup_with_refresh)
    monkeypatch.setattr(sdk_plugin, "invoke", record_invoke)
    monkeypatch.setattr(sdk_plugin, "snapshot_device", record_snapshot)
    if endpoint == "invoke":
        response = await async_client.post(
            f"/api/v1/printers/{printer_id}/operations/tool.select",
            json={"resource_id": "tool:violet", "request_id": "refresh-during-lookup", "parameters": {}},
        )
        assert response.status_code == 200, response.text
        assert invoked_connections == [original._connection]
        assert len(sdk_plugin.calls) == 1 and updated.calls == []
        assert original._connection.selected_tool == "tool:violet"
        assert getattr(printer_manager.get_client(printer_id)._connection, "selected_tool", None) is None
        row = await db_session.get(PrinterOperation, response.json()["task_id"])
        assert row.operation_schema["extensions"]["bambuddy:provider"]["version"] == sdk_plugin.version
    else:
        response = await async_client.get(f"/api/v1/printers/{printer_id}/device-state")
        assert response.status_code == 200, response.text
        assert snapshot_connections == [original._connection]
        assert invoked_connections == []
        assert sdk_plugin.calls == updated.calls == []
    assert switched
    assert printer_manager.get_client(printer_id)._plugin is updated
    assert printer_manager.retained_connection(printer_id, sdk_plugin) is original


@pytest.mark.parametrize("endpoint", ["invoke", "snapshot", "descriptor"])
async def test_identity_change_after_request_capture_rejects_stale_transport(
    async_client, sdk_plugin, monkeypatch, endpoint
):
    from sqlalchemy import update

    from backend.app.api.routes import printer_devices
    from backend.app.models.printer import Printer
    from backend.app.services.printer_manager import printer_manager

    printer_id = await create_sdk_printer(async_client)
    original = printer_manager.get_client(printer_id)
    original_lookup = printer_devices._printer

    async def lookup_after_edit(db, target_id, *, lock=False):
        # The HTTP dependency has captured the adapter before the awaited
        # database lookup sees an administrator's changed physical identity.
        await db.execute(update(Printer).where(Printer.id == target_id).values(serial_number="REPLACED-DEVICE"))
        await db.commit()
        return await original_lookup(db, target_id, lock=lock)

    monkeypatch.setattr(printer_devices, "_printer", lookup_after_edit)
    if endpoint == "invoke":
        response = await async_client.post(
            f"/api/v1/printers/{printer_id}/operations/tool.select",
            json={"resource_id": "tool:violet", "parameters": {}},
        )
    else:
        suffix = "device-state" if endpoint == "snapshot" else "device"
        response = await async_client.get(f"/api/v1/printers/{printer_id}/{suffix}")
    assert response.status_code == 409, response.text
    assert "configuration changed" in response.json()["detail"].lower()
    assert sdk_plugin.calls == []
    assert getattr(original._connection, "selected_tool", None) is None


async def invoke(client, printer_id, operation, parameters=None, resource=None, headers=None, **fields):
    return await client.post(
        f"/api/v1/printers/{printer_id}/operations/{operation}",
        headers=headers or {},
        json={"parameters": parameters or {}, "resource_id": resource, **fields},
    )


async def test_six_tool_usb_printer_descriptors_and_high_temperature_limits(async_client, sdk_plugin):
    printer_id = await create_sdk_printer(async_client)
    descriptor = await async_client.get(f"/api/v1/printers/{printer_id}/device")
    assert descriptor.status_code == 200, descriptor.text
    assert sum(resource["kind"] == "tool" for resource in descriptor.json()["resources"]) == 6
    assert "tool:violet" in {resource["id"] for resource in descriptor.json()["resources"]}
    assert "CONFIG-SECRET" not in descriptor.text
    accepted = await invoke(async_client, printer_id, "heater.set_target", {"target": 520}, "heater:5")
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "succeeded"
    calls = len(sdk_plugin.calls)
    rejected = await invoke(async_client, printer_id, "heater.set_target", {"target": 521}, "heater:5")
    assert rejected.status_code == 422
    assert len(sdk_plugin.calls) == calls
    selected = await invoke(async_client, printer_id, "tool.select", resource="tool:violet")
    assert selected.status_code == 200, selected.text
    assert sdk_plugin.connections[-1].selected_tool == "tool:violet"
    state = await async_client.get(f"/api/v1/printers/{printer_id}/device-state")
    assert state.status_code == 200, state.text
    assert state.json()["schema_version"] == 2


async def test_invalid_plugin_result_is_a_structured_failure(async_client, sdk_plugin):
    printer_id = await create_sdk_printer(async_client)
    result = await invoke(async_client, printer_id, "test.invalid_result")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "failed"
    assert result.json()["error"]["code"] == "invalid_plugin_result"
    assert "invalid-boolean-result" not in result.text


async def test_async_operation_poll_cancel_and_terminal_idempotence(async_client, sdk_plugin):
    printer_id = await create_sdk_printer(async_client)
    started = await invoke(async_client, printer_id, "task.calibrate", request_id="calibration-1")
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "running"
    task_id = started.json()["task_id"]
    polled = await async_client.get(f"/api/v1/printers/{printer_id}/operation-tasks/{task_id}")
    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] == "running"
    cancelled = await async_client.post(f"/api/v1/printers/{printer_id}/operation-tasks/{task_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert (
        await async_client.post(f"/api/v1/printers/{printer_id}/operation-tasks/{task_id}/cancel")
    ).json() == cancelled.json()
    completed = await invoke(async_client, printer_id, "task.calibrate", request_id="calibration-complete")
    provider_id = "provider:calibration-complete"
    sdk_plugin.tasks[provider_id] = sdk_plugin.tasks[provider_id].model_copy(
        update={"status": "succeeded", "result": True, "updated_at": datetime.now(timezone.utc)}
    )
    completed_task = await async_client.get(
        f"/api/v1/printers/{printer_id}/operation-tasks/{completed.json()['task_id']}"
    )
    assert completed_task.status_code == 200, completed_task.text
    assert completed_task.json()["status"] == "succeeded"
    assert completed_task.json()["result"] is True


async def test_idempotency_survives_process_identity_and_rejects_conflicting_reuse(
    async_client, sdk_plugin, monkeypatch
):
    from backend.app.api.routes import printer_devices

    printer_id = await create_sdk_printer(async_client)
    first = await invoke(async_client, printer_id, "tool.select", resource="tool:violet", idempotency_key="stable-key")
    assert first.status_code == 200, first.text
    count = len(sdk_plugin.calls)
    monkeypatch.setattr(printer_devices, "_PROCESS_ID", "simulated-restart")
    duplicate = await invoke(
        async_client, printer_id, "tool.select", resource="tool:violet", idempotency_key="stable-key"
    )
    assert duplicate.json() == first.json()
    assert len(sdk_plugin.calls) == count
    conflict = await invoke(async_client, printer_id, "tool.select", resource="tool:0", idempotency_key="stable-key")
    assert conflict.status_code == 409
    assert len(sdk_plugin.calls) == count


async def test_interrupted_operation_is_unknown_and_never_automatically_replayed(async_client, sdk_plugin, db_session):
    from backend.app.models.printer_operation import PrinterOperation

    printer_id = await create_sdk_printer(async_client)
    first = await invoke(
        async_client, printer_id, "tool.select", resource="tool:violet", idempotency_key="interrupted-key"
    )
    row = await db_session.scalar(select(PrinterOperation).where(PrinterOperation.id == first.json()["task_id"]))
    row.process_id = "old-process"
    row.provider_task_id = None
    row.receipt = OperationReceipt(
        operation_id=row.operation_id, request_id=row.request_data["request_id"], status="accepted", task_id=row.id
    ).model_dump(mode="json")
    await db_session.commit()
    calls = len(sdk_plugin.calls)
    result = await async_client.get(f"/api/v1/printers/{printer_id}/operation-tasks/{row.id}")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "failed"
    assert result.json()["error"]["code"] == "outcome_unknown"
    duplicate = await invoke(
        async_client, printer_id, "tool.select", resource="tool:violet", idempotency_key="interrupted-key"
    )
    assert duplicate.json()["error"]["code"] == "outcome_unknown"
    assert len(sdk_plugin.calls) == calls


async def test_dynamic_permissions_scope_and_telemetry_redaction_preserve_driver_state(
    async_client, sdk_plugin, printer_factory
):
    printer_id = await create_sdk_printer(async_client)
    hidden = await printer_factory(plugin_id=sdk_plugin.id, model="simulator", plugin_options={"token": "hidden"})
    admin = await _admin_token(async_client)
    viewer_group = await _group(
        async_client, admin, "sdk-viewers", permissions=["printers:read"], printer_ids=[printer_id]
    )
    operator_group = await _group(
        async_client,
        admin,
        "sdk-operators",
        permissions=["printers:read", "printers:control"],
        printer_ids=[printer_id],
    )
    viewer, _ = await _user(async_client, admin, "sdkviewer", [viewer_group])
    operator, _ = await _user(async_client, admin, "sdkoperator", [operator_group])
    assert (await async_client.get(f"/api/v1/printers/{printer_id}/device")).status_code == 401
    assert (await async_client.get(f"/api/v1/printers/{hidden.id}/device", headers=_auth(viewer))).status_code == 404
    assert (
        await invoke(async_client, printer_id, "tool.select", resource="tool:violet", headers=_auth(viewer))
    ).status_code == 403
    assert (
        await invoke(async_client, printer_id, "tool.select", resource="tool:violet", headers=_auth(operator))
    ).status_code == 200
    assert (await invoke(async_client, printer_id, "test.dual_scope", headers=_auth(operator))).status_code == 403
    assert (await invoke(async_client, printer_id, "test.dual_scope", headers=_auth(admin))).status_code == 200
    visible = await async_client.get(f"/api/v1/printers/{printer_id}/device-state", headers=_auth(viewer))
    assert visible.status_code == 200, visible.text
    assert "PRIVATE-TELEMETRY" not in visible.text
    raw = next(state for state in sdk_plugin.connections[-1].state.resources if state.resource_id == "sensor:private")
    assert raw.properties["value"].value == "PRIVATE-TELEMETRY"
    full = await async_client.get(f"/api/v1/printers/{printer_id}/device-state", headers=_auth(admin))
    assert "PRIVATE-TELEMETRY" in full.text


async def test_jobs_use_host_dispatch_and_http_cannot_bypass_queue_admission(async_client, sdk_plugin):
    from backend.app.printers.api import execute_host_command
    from backend.app.services.printer_manager import printer_manager

    printer_id = await create_sdk_printer(async_client)
    job = JobRequest(
        job_id="neutral-job",
        artifact=JobArtifact(uri="host://artifact/1", format="gcode"),
        selected_tool_ids=["tool:violet"],
    )
    denied = await invoke(async_client, printer_id, "job.start", job.model_dump(mode="json"))
    assert denied.status_code == 409
    assert await execute_host_command(
        printer_manager.get_client(printer_id), "start_print", filename="prepared.gcode", job=job
    )
    assert sdk_plugin.connections[-1].state.job.active_tool_ids == ["tool:violet"]
    assert await execute_host_command(printer_manager.get_client(printer_id), "pause_print")
    assert sdk_plugin.connections[-1].state.job.state == "paused"


async def test_public_job_controls_dispatch_to_api2_operations(async_client, sdk_plugin):
    from backend.app.printers.api import execute_host_command
    from backend.app.services.printer_manager import printer_manager

    printer_id = await create_sdk_printer(async_client)
    job = JobRequest(
        job_id="public-controls",
        artifact=JobArtifact(uri="host://artifact/1", format="gcode"),
        selected_tool_ids=["tool:violet"],
    )
    assert await execute_host_command(
        printer_manager.get_client(printer_id), "start_print", filename="prepared.gcode", job=job
    )
    for control, expected in (("pause", "job.pause"), ("resume", "job.resume"), ("stop", "job.stop")):
        response = await async_client.post(f"/api/v1/printers/{printer_id}/print/{control}")
        assert response.status_code == 200, response.text
        assert sdk_plugin.calls[-1].operation_id == expected
