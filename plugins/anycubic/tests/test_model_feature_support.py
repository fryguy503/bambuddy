"""A transport field must not invent hardware absent from the selected model."""

from dataclasses import replace

import pytest

from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.sdk import validate_device, validate_snapshot
from backend.app.printers.sdk.host import state_for_host
from plugins.anycubic.payload.models import get_model
from plugins.anycubic.payload.plugin import AnycubicPrinterPlugin


def configuration(model="Kobra S1"):
    return PrinterConfig(
        "192.168.50.50",
        "MODEL-CAPABILITIES",
        "",
        model=model,
        plugin_id="anycubic",
        options={"tls_fingerprint": "a" * 64},
    )


@pytest.mark.parametrize("connected", [False, True])
@pytest.mark.parametrize("reported", [None, 0, 42])
def test_ks1_never_exposes_nonexistent_chamber_temperature_or_heater(connected, reported):
    plugin = AnycubicPrinterPlugin()
    config = configuration()
    connection = plugin.create_connection(config)
    connection.native.state.connected = connected
    if reported is not None:
        connection.native.state.temperatures.update(chamber=reported, chamber_target=60)
    descriptor = validate_device(plugin.describe_device(config))
    assert not any(
        "chamber" in resource.roles and resource.kind in {"sensor", "heater"} for resource in descriptor.resources
    )
    assert descriptor.resource("light:chamber")
    assert {resource.id for resource in descriptor.resources if resource.kind == "heater"} == {
        "heater:nozzle",
        "heater:bed",
    }
    snapshot = validate_snapshot(descriptor, connection.state)
    assert not any(resource.resource_id in {"sensor:chamber", "heater:chamber"} for resource in snapshot.resources)
    host = state_for_host(config, plugin, snapshot)
    assert "chamber" not in host.temperatures and "chamber_target" not in host.temperatures


def test_unknown_model_sensor_is_not_assumed_but_explicitly_reported_sensor_is_read_only():
    plugin = AnycubicPrinterPlugin()
    config = configuration("Kobra S1 Max")
    connection = plugin.create_connection(config)
    assert get_model(config.model).chamber_temperature is None
    assert "sensor:chamber" not in {resource.id for resource in plugin.describe_device(config).resources}
    connection.native.state.temperatures["chamber"] = 35
    descriptor = plugin.describe_device(config)
    sensor = descriptor.resource("sensor:chamber")
    assert not sensor.properties["temperature"].writable
    assert not any(resource.id == "heater:chamber" for resource in descriptor.resources)


def test_unknown_model_and_unreported_features_are_not_copied_from_another_printer():
    plugin = AnycubicPrinterPlugin()
    with pytest.raises(ValueError):
        plugin.describe_device(replace(configuration(), model="Future unreviewed printer"))
