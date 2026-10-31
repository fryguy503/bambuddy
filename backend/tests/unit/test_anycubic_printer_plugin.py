"""Install the real Anycubic archive and exercise its public SDK contract offline."""

import hashlib
import io
import json
import shutil
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.core.config import settings
from backend.app.printers.contracts import PrinterConfig, UnknownPrinterPlugin
from backend.app.printers.management import PluginManager
from backend.app.printers.packages import MAX_PACKAGE_BYTES, PluginPackageError, inspect_package
from backend.app.printers.registry import PrinterPluginRegistry
from backend.app.printers.sdk import (
    JobArtifact,
    JobRequest,
    MaterialBinding,
    OperationRequest,
    validate_device,
    validate_invocation,
    validate_job_request,
    validate_receipt,
    validate_snapshot,
)
from plugins.anycubic.build import SOURCE, build

MODELS = ("Kobra S1", "Kobra S1 Max", "Kobra 3", "Kobra 3 V2", "Kobra 3 Max", "Kobra 4", "Kobra X")


def config_for(model="Kobra S1"):
    return PrinterConfig(
        "192.168.50.50",
        "SDK-ANYCUBIC",
        "12345678",
        model=model,
        plugin_id="anycubic",
        printer_id=42,
        options={"tls_fingerprint": "a" * 64},
    )


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    output = tmp_path_factory.mktemp("anycubic-release") / "anycubic-0.1.4.bbp"
    manifest = build(SOURCE, output)
    return manifest, output


@pytest.fixture
def installed(release, tmp_path, tmp_path_factory, monkeypatch):
    from backend.app.printers import registry as registry_module

    monkeypatch.setattr(settings, "printer_plugin_dir", tmp_path / "installed-plugins")
    # Journal file names are hashes; keep the test's extra path segments short
    # enough for Windows rename APIs as well as the Linux release environment.
    monkeypatch.setattr(settings, "base_dir", tmp_path_factory.mktemp("r"))
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    before = PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", before)
    assert {plugin.id for plugin in before.all()} == {"bambu"}
    manager = PluginManager()
    manifest, output = release
    staged = manager.stage(output.read_bytes())
    assert (staged["plugin_id"], staged["version"], staged["activation"]) == ("anycubic", "0.1.4", "staged")
    with pytest.raises(UnknownPrinterPlugin):
        before.get("anycubic")
    manager.select("anycubic", manifest.version)
    with pytest.raises(UnknownPrinterPlugin):
        before.get("anycubic")
    restarted = PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", restarted)
    plugin = restarted.get("anycubic")
    assert {item.id for item in restarted.all()} == {"bambu", "anycubic"}
    assert plugin.__class__.__module__.startswith("bambuddy_printer_plugins.anycubic_")
    return plugin, restarted


def test_release_inventory_images_and_identity_are_installable(release):
    manifest, output = release
    inspected = inspect_package(output.read_bytes())
    assert (manifest.plugin_id, manifest.version, manifest.publisher_id, manifest.api_version) == (
        "anycubic",
        "0.1.4",
        "fryguy503",
        2,
    )
    assert manifest.entrypoint == "plugin:AnycubicPrinterPlugin"
    assert manifest.license == "AGPL-3.0-only"
    assert {"aiohttp", "cryptography", "httpx", "paho-mqtt"} <= manifest.dependencies.keys()
    assert len(manifest.models) == 7
    assert output.stat().st_size < min(MAX_PACKAGE_BYTES, 100 * 1024 * 1024)
    assert inspected.manifest == manifest
    assert len([name for name in manifest.files if name.startswith("assets/printers/")]) == 7
    assert manifest.requires_api == ">=2.2.0,<3.0.0"
    expected = hashlib.sha256(output.read_bytes()).hexdigest()
    assert output.with_suffix(".bbp.sha256").read_text(encoding="utf-8") == f"{expected}  {output.name}\n"
    with zipfile.ZipFile(output) as archive:
        for name, digest in manifest.files.items():
            content = archive.read("payload/" + name)
            assert hashlib.sha256(content).hexdigest() == digest
            if name.endswith((".py", ".json")):
                assert b"\r" not in content
        assert json.loads(archive.read("manifest.json"))["plugin_id"] == "anycubic"
        assert archive.read("payload/LICENSE") == (SOURCE.parents[2] / "LICENSE").read_bytes().replace(
            b"\r\n", b"\n"
        ).replace(b"\r", b"\n")


def test_committed_release_matches_current_payload(release):
    _manifest, rebuilt = release
    committed = SOURCE.parents[2] / "packages" / "anycubic" / "anycubic-0.1.4.bbp"
    assert committed.is_file(), "Run python plugins/anycubic/build.py before publishing this release"
    assert committed.read_bytes() == rebuilt.read_bytes(), "Rebuild the release after changing its payload"
    expected = hashlib.sha256(committed.read_bytes()).hexdigest()
    assert committed.with_suffix(".bbp.sha256").read_text(encoding="utf-8") == f"{expected}  {committed.name}\n"


def test_published_010_manual_selection_loads_current_release_on_new_registry(release, tmp_path, monkeypatch):
    from backend.app.printers import registry as registry_module

    monkeypatch.setattr(settings, "printer_plugin_dir", tmp_path / "plugins")
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    manager = PluginManager()
    original = SOURCE.parents[2] / "packages" / "anycubic" / "anycubic-0.1.0.bbp"
    original_bytes = original.read_bytes()
    assert (
        hashlib.sha256(original_bytes).hexdigest() == "6cffe5bf562ae3397a228d0b2a2c8ba4801822119d2679345489866befd4babb"
    )
    manager.stage(original_bytes)
    manager.select("anycubic", "0.1.0")
    running = PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", running)
    assert running.get("anycubic").version == "0.1.0"
    _, updated = release
    manager.stage(updated.read_bytes())
    manager.select("anycubic", "0.1.4", loaded_version="0.1.0")
    assert running.get("anycubic").version == "0.1.0"
    restarted = PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", restarted)
    assert restarted.get("anycubic").version == "0.1.4"
    assert restarted.get("anycubic").__class__.__module__ != running.get("anycubic").__class__.__module__
    state = manager.read_state()["plugins"]["anycubic"]
    assert {"0.1.0", "0.1.4"} <= state["versions"].keys()
    assert state["previous_version"] == "0.1.0"
    assert original.read_bytes() == original_bytes


def test_previously_published_packages_remain_immutable():
    for version, expected in {
        "0.1.0": "6cffe5bf562ae3397a228d0b2a2c8ba4801822119d2679345489866befd4babb",
        "0.1.1": "4271bbd85321b31484c399b691193e8ad1eb6a943797c9bc420c8c8619269587",
        "0.1.2": "fb316fc54581b3e1f75f9da3eb86a2032851faf1216a813b76f7544db1b3f36a",
        "0.1.3": "85376ab7f57c0df9e6fa7d974d55bcd27b3f6ecba3925470e886439e724d3c3d",
    }.items():
        package = SOURCE.parents[2] / "packages" / "anycubic" / f"anycubic-{version}.bbp"
        assert hashlib.sha256(package.read_bytes()).hexdigest() == expected


def test_release_is_identical_for_lf_and_crlf_checkouts(tmp_path):
    outputs = []
    for label, newline in (("lf", b"\n"), ("crlf", b"\r\n")):
        source = tmp_path / label / "payload"
        shutil.copytree(SOURCE, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for path in source.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".json"}:
                canonical = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                path.write_bytes(canonical.replace(b"\n", newline))
        output = tmp_path / f"{label}.bbp"
        build(source, output)
        outputs.append(output.read_bytes())
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("change", ["missing", "checksum"])
def test_invalid_release_payload_is_rejected_before_staging(release, tmp_path, change):
    _manifest, output = release
    changed = io.BytesIO()
    with zipfile.ZipFile(output) as original, zipfile.ZipFile(changed, "w", zipfile.ZIP_DEFLATED) as modified:
        for name in original.namelist():
            if change == "missing" and name == "payload/LICENSE":
                continue
            content = original.read(name)
            if change == "checksum" and name == "payload/plugin.py":
                content += b"\n# modified after release\n"
            modified.writestr(name, content)
    manager = PluginManager(tmp_path / "rejected")
    with pytest.raises(PluginPackageError, match="inventory|checksum"):
        manager.stage(changed.getvalue())
    assert not manager.state_path.exists()


def test_expected_archive_checksum_is_enforced(release):
    _manifest, output = release
    with pytest.raises(PluginPackageError, match="SHA-256"):
        inspect_package(output.read_bytes(), expected_sha256="0" * 64)


@pytest.mark.parametrize("model", MODELS)
def test_every_installed_model_has_valid_resources_operations_and_material_jobs(installed, model):
    plugin, _registry = installed
    config = config_for(model)
    device = validate_device(plugin.describe_device(config))
    assert device.model_id is not None
    tools = [resource for resource in device.resources if resource.kind == "tool"]
    materials = [resource for resource in device.resources if resource.kind == "material_source"]
    assert tools and materials
    assert {"heater.set_target", "job.start", "job.pause", "job.resume", "job.stop"}.issubset(
        operation.id for operation in device.operations
    )
    for heater in (resource for resource in device.resources if resource.kind == "heater"):
        bound = heater.properties["target"].value_schema["maximum"]
        validate_invocation(
            device,
            OperationRequest(operation_id="heater.set_target", resource_id=heater.id, parameters={"target": bound}),
        )
        with pytest.raises(ValueError):
            validate_invocation(
                device,
                OperationRequest(
                    operation_id="heater.set_target", resource_id=heater.id, parameters={"target": bound + 1}
                ),
            )
    job = JobRequest(
        job_id="anycubic-sdk-job:" + "j" * 80,
        artifact=JobArtifact(uri="host://uploads/fixture.gcode", format="gcode", filename="fixture.gcode"),
        selected_tool_ids=[tools[0].id],
        bindings=[MaterialBinding(input_id="T0", tool_id=tools[0].id, source_id=materials[0].id)],
    )
    assert validate_job_request(device, job).job_id == job.job_id
    invalid = job.model_copy(update={"selected_tool_ids": ["tool:not-installed"]})
    with pytest.raises(ValueError):
        validate_job_request(device, invalid)
    connection = plugin.create_connection(config)
    live_device = validate_device(plugin.describe_device(config))
    state = validate_snapshot(live_device, plugin.snapshot_device(config, connection))
    assert state.device_id == device.device_id
    assert state.connected is False


@pytest.mark.asyncio
async def test_sdk_heater_command_uses_native_wire_codec_and_enforces_model_limit(installed):
    plugin, _registry = installed
    config = config_for("Kobra S1 Max")
    connection = plugin.create_connection(config)
    native = connection.native
    frames = []

    class Transport:
        def publish(self, topic, payload, *, qos, retain):
            frames.append((topic, json.loads(payload), qos, retain))
            return SimpleNamespace(rc=0)

    native._client = Transport()
    native._subscribed = True
    native._device_id = "sdk-device"
    native.state.connected = True
    native.state.state = "IDLE"
    native._last_state_time = native._last_message_monotonic = time.monotonic()
    request = OperationRequest(
        operation_id="heater.set_target", resource_id="heater:nozzle", parameters={"target": 340}
    )
    device = plugin.describe_device(config)
    operation = validate_invocation(device, request)
    receipt = validate_receipt(operation, request, await plugin.invoke(config, connection, request))
    assert receipt.status == "accepted"
    assert receipt.result["hardware_confirmed"] is False
    assert frames
    topic, envelope, qos, retain = frames[0]
    assert topic.endswith("/20029/sdk-device/tempature")
    assert envelope["action"] == "set"
    assert envelope["data"] == {"type": 0, "target_nozzle_temp": 340, "target_hotbed_temp": 0}
    assert qos == 0 and retain is False
    count = len(frames)
    with pytest.raises(ValueError):
        await plugin.invoke(config, connection, request.model_copy(update={"parameters": {"target": 351}}))
    assert len(frames) == count
    waiting = await plugin.operation_status(config, connection, receipt.task_id)
    assert waiting.status == "accepted"
    native.state.temperatures["nozzle_target"] = 340
    native._last_state_time = native._last_message_monotonic = time.monotonic()
    confirmed = await plugin.operation_status(config, connection, receipt.task_id)
    assert confirmed.status == "succeeded"
    assert confirmed.result["hardware_confirmed"] is True


def dispatch_case(installed, tmp_path, monkeypatch, *, acknowledge):
    from importlib import import_module

    plugin, registry = installed
    config = config_for()
    events = []
    connection = plugin.create_connection(config, on_event=events.append)
    namespace = registry.loaded_sources["anycubic"]["module_namespace"]
    inventory = import_module(namespace + ".inventory")
    monkeypatch.setattr(inventory, "capture", AsyncMock(return_value={"version": 1, "bindings": [], "skipped": []}))
    source = tmp_path / "fixture.gcode"
    source.write_bytes(
        b"; generated by OrcaSlicer\n; printer_model = Anycubic Kobra S1\n"
        b"; filament_type = PLA\n; filament_colour = #FF0000\n; filament used [mm] = 100\nT0\nG1 X0\n"
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    uri = "uploaded-fixture.gcode"
    connection.journal.save_artifact(
        connection.printer_key,
        uri,
        {
            "path": str(source),
            "source_filename": source.name,
            "native_filename": "uploaded-fixture.gcode",
            "sha256": digest,
            "estimates": {},
        },
    )
    job = JobRequest(
        job_id="queue:isolated-anycubic",
        artifact=JobArtifact(uri=uri, format="gcode", sha256=digest),
        selected_tool_ids=["tool:primary"],
    )
    frames = []
    control = {"job_id": job.job_id, "acknowledge": acknowledge}

    def ready(target):
        native = target.native

        class Broker:
            def publish(self, topic, payload, *, qos, retain):
                envelope = json.loads(payload)
                frames.append((topic, envelope))
                if envelope["type"] == "print" and envelope["action"] == "start":
                    assert target.journal.get(control["job_id"])["status"] == "intent"
                    if control["acknowledge"]:
                        native._print_report(
                            "print",
                            {
                                "action": "start",
                                "msgid": envelope["msgid"],
                                "code": 200,
                                "data": {"taskid": "native-task-9", "filename": envelope["data"]["filename"]},
                            },
                        )
                    else:
                        # The session ends without an acknowledgement. The real
                        # codec wakes its pending wait and retains uncertainty.
                        native._reset_print_request()
                return SimpleNamespace(rc=0)

        native._client = Broker()
        native._subscribed = True
        native._device_id = "sdk-device"
        native.state.connected = True
        native.state.state = "IDLE"
        native._last_state_time = native._last_message_monotonic = native._info_observed = time.monotonic()
        return native

    return SimpleNamespace(
        plugin=plugin,
        config=config,
        connection=connection,
        native=ready(connection),
        job=job,
        frames=frames,
        events=events,
        ready=ready,
        control=control,
    )


@pytest.mark.asyncio
async def test_sdk_job_uses_real_native_ack_and_persisted_intent_without_replay(installed, tmp_path, monkeypatch):
    case = dispatch_case(installed, tmp_path, monkeypatch, acknowledge=True)
    request = OperationRequest(
        operation_id="job.start", request_id="sdk-operation-request", parameters=case.job.model_dump(mode="json")
    )
    operation = validate_invocation(case.plugin.describe_device(case.config), request)
    receipt = validate_receipt(operation, request, await case.plugin.invoke(case.config, case.connection, request))
    assert receipt.status == "accepted" and receipt.task_id == case.job.job_id
    assert receipt.result["hardware_confirmed"] is False
    assert len(case.frames) == 1
    envelope = case.frames[0][1]
    assert envelope["action"] == "start" and envelope["data"]["filename"] == "uploaded-fixture.gcode"
    assert envelope["data"]["ams_settings"]["use_ams"] is False
    record = case.connection.journal.get(case.job.job_id)
    assert record["native_job_id"] == "native-task-9"
    assert record["request_id"] == request.request_id

    case.native.state.state = "RUNNING"
    case.native.state.gcode_file = "uploaded-fixture.gcode"
    case.native.state.subtask_id = "native-task-9"
    case.native._telemetry.lifecycle_observed = True
    case.native._telemetry.phase = "printing"
    case.native._telemetry.task_id = "native-task-9"
    case.connection._observe_jobs()
    running = validate_receipt(
        operation, request, await case.plugin.operation_status(case.config, case.connection, receipt.task_id)
    )
    assert running.status == "running"
    assert any(event.event == "job.started" for event in case.events)

    restarted = case.plugin.create_connection(case.config)
    case.ready(restarted)
    duplicate = await case.plugin.invoke(case.config, restarted, request)
    assert duplicate.status == "running"
    assert len(case.frames) == 1
    new_job = case.job.model_copy(update={"job_id": "queue:another-anycubic"})
    rejected = await case.plugin.invoke(
        case.config, restarted, OperationRequest(operation_id="job.start", parameters=new_job.model_dump(mode="json"))
    )
    assert rejected.status == "failed" and len(case.frames) == 1


@pytest.mark.asyncio
async def test_unknown_dispatch_survives_restart_and_requires_explicit_idle_resolution(
    installed, tmp_path, monkeypatch
):
    case = dispatch_case(installed, tmp_path, monkeypatch, acknowledge=False)
    request = OperationRequest(operation_id="job.start", parameters=case.job.model_dump(mode="json"))
    receipt = await case.plugin.invoke(case.config, case.connection, request)
    assert receipt.status == "failed"
    assert case.connection.journal.get(case.job.job_id)["status"] == "unknown"
    assert len(case.frames) == 1
    restarted = case.plugin.create_connection(case.config)
    case.ready(restarted)
    duplicate = await case.plugin.invoke(case.config, restarted, request)
    assert duplicate.status == "failed" and len(case.frames) == 1
    resolve = OperationRequest(operation_id="anycubic.job.resolve", parameters={"job_id": case.job.job_id})
    assert (
        await case.plugin.invoke(case.config, restarted, OperationRequest(operation_id="job.clear_plate"))
    ).status == "failed"
    restarted.native.state.state = "RUNNING"
    assert (await case.plugin.invoke(case.config, restarted, resolve)).status == "failed"
    restarted.native.state.state = "IDLE"
    restarted.native._last_state_time = restarted.native._last_message_monotonic = time.monotonic() - 120
    assert (await case.plugin.invoke(case.config, restarted, resolve)).status == "failed"
    restarted.native._last_state_time = restarted.native._last_message_monotonic = time.monotonic()
    assert case.connection.journal.get(case.job.job_id)["status"] == "unknown"
    assert (await case.plugin.invoke(case.config, restarted, resolve)).status == "succeeded"
    assert case.connection.journal.get(case.job.job_id)["status"] == "resolved"
    new_job = case.job.model_copy(update={"job_id": "queue:reviewed-new-dispatch"})
    held = await case.plugin.invoke(
        case.config, restarted, OperationRequest(operation_id="job.start", parameters=new_job.model_dump(mode="json"))
    )
    assert held.status == "failed" and len(case.frames) == 1
    assert (
        await case.plugin.invoke(case.config, restarted, OperationRequest(operation_id="job.clear_plate"))
    ).status == "succeeded"
    assert (await case.plugin.invoke(case.config, restarted, request)).status == "failed"
    assert len(case.frames) == 1  # Resolving does not authorize replay of the old identity.
    case.control.update(job_id=new_job.job_id, acknowledge=True)
    new_receipt = await case.plugin.invoke(
        case.config, restarted, OperationRequest(operation_id="job.start", parameters=new_job.model_dump(mode="json"))
    )
    assert new_receipt.status == "accepted"
    assert len(case.frames) == 2
