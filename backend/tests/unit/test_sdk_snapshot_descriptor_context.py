"""Native capability updates cannot invalidate an already accepted report."""

from unittest.mock import Mock

import pytest

from backend.app.printers import api as api_module
from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.sdk import (
    DeviceEvent,
    JobProgress,
    OperationRequest,
    ResourceDescriptor,
    ResourceProperty,
    ResourceState,
    SDKValidationError,
    TelemetryValue,
)
from examples.printer_plugins.reference.payload.plugin import ReferencePlugin


class DynamicPrinter(ReferencePlugin):
    limit = 300
    fan_present = False
    wrong_device = False

    def describe_device(self, config):
        device = super().describe_device(config)
        device.revision = f"native:{self.limit}:{self.fan_present}"
        device.resource("heater:0").properties["target"].value_schema["maximum"] = self.limit
        if self.fan_present:
            device.resources.append(
                ResourceDescriptor(
                    id="fan:observed",
                    kind="fan",
                    label="Reported fan",
                    properties={
                        "percent": ResourceProperty(
                            value_schema={"type": "number", "minimum": 0, "maximum": 100}, unit="%"
                        )
                    },
                )
            )
        return device

    def snapshot_device(self, config, connection):
        device = self.describe_device(config)
        snapshot = connection.state.model_copy(deep=True)
        snapshot.descriptor_revision = device.revision
        snapshot.device_id = "another-printer" if self.wrong_device else device.device_id
        next(state for state in snapshot.resources if state.resource_id == "heater:0").properties["target"].value = 200
        if self.fan_present:
            snapshot.resources.append(
                ResourceState(resource_id="fan:observed", properties={"percent": TelemetryValue(value=25, unit="%")})
            )
        return snapshot


@pytest.fixture
def dynamic(monkeypatch):
    plugin = DynamicPrinter()
    config = PrinterConfig("", "dynamic-six", "", model="simulator", plugin_id=plugin.id, options={"tool_count": 6})
    monkeypatch.setattr(api_module, "plugin_for", lambda printer: plugin)
    adapter = api_module.printer_api.create_connection(config, provider=plugin, on_state_change=Mock())
    raw = adapter._connection
    next(state for state in raw.state.resources if state.resource_id == "heater:0").properties["target"].value = 280
    raw.connect()
    return config, plugin, adapter


def test_cached_host_snapshot_uses_its_accepted_contract_after_native_limits_change(dynamic):
    _config, plugin, adapter = dynamic
    plugin.limit = 250
    plugin.fan_present = True
    # The old target remains valid under the contract that accompanied it.
    state = adapter.state
    assert state.connected is True
    assert state.device_snapshot.descriptor_revision == "native:300:False"
    assert state.temperatures["heater:0_target"] == 280
    assert adapter._event_bridge.snapshot_context()[1].revision == "native:300:False"


def test_resource_api_refreshes_old_cache_against_current_native_capabilities(dynamic):
    config, plugin, adapter = dynamic
    plugin.limit = 250
    plugin.fan_present = True
    snapshot = api_module.printer_api.snapshot_device(config, adapter)
    assert snapshot.descriptor_revision == "native:250:True"
    assert (
        next(state for state in snapshot.resources if state.resource_id == "fan:observed").properties["percent"].value
        == 25
    )
    assert (
        next(state for state in snapshot.resources if state.resource_id == "heater:0").properties["target"].value == 200
    )


async def test_current_operation_validation_still_enforces_new_native_limit(dynamic):
    config, plugin, adapter = dynamic
    plugin.limit = 250
    with pytest.raises(SDKValidationError):
        await api_module.printer_api.invoke(
            config,
            adapter,
            OperationRequest(operation_id="heater.set_target", resource_id="heater:0", parameters={"target": 280}),
        )


def test_wrong_printer_snapshot_is_still_rejected_after_capability_refresh(dynamic):
    config, plugin, adapter = dynamic
    plugin.limit = 250
    plugin.wrong_device = True
    with pytest.raises(SDKValidationError, match="identity"):
        api_module.printer_api.snapshot_device(config, adapter)


def test_fresh_snapshot_retry_is_bounded_and_does_not_ignore_revision_failure(dynamic, monkeypatch):
    config, plugin, adapter = dynamic
    plugin.limit = 250
    stale = adapter._connection.state.model_copy(deep=True)
    read = Mock(return_value=stale)
    monkeypatch.setattr(plugin, "snapshot_device", read)
    with pytest.raises(SDKValidationError, match="descriptor revision"):
        api_module.printer_api.snapshot_device(config, adapter)
    assert read.call_count == 2


def test_lifecycle_without_new_telemetry_keeps_matching_cached_resource_contract(dynamic):
    _config, plugin, adapter = dynamic
    plugin.limit = 250
    plugin.fan_present = True
    bridge = adapter._event_bridge
    job = JobProgress(job_id="dynamic-job", state="printing", active_tool_ids=["tool:0"])
    assert bridge.emit(
        DeviceEvent(device_id="dynamic-six", sequence=2, event="job.started", data={"job": job.model_dump(mode="json")})
    )
    assert adapter.state.subtask_id == "dynamic-job"
    snapshot, descriptor = bridge.snapshot_context()
    assert snapshot.descriptor_revision == descriptor.revision == "native:300:False"
