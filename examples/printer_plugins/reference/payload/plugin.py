"""Runnable API2 authoring fixture: arbitrary tools, no hardware or sockets."""

from backend.app.printers.contracts import PrinterModel, PrinterPlugin
from backend.app.printers.sdk import (
    DeviceDescriptor,
    DeviceEvent,
    DeviceSnapshot,
    JobEventData,
    JobProgress,
    JobRequest,
    OperationDescriptor,
    ResourceDescriptor,
    ResourceProperty,
    ResourceState,
    TelemetryValue,
    validate_job_request,
)
from backend.app.printers.sdk.domain import utc_now


class ReferenceConnection:
    def __init__(self, descriptor, callbacks):
        self.descriptor = descriptor
        self.callbacks = callbacks
        self.state = DeviceSnapshot(
            device_id=descriptor.device_id,
            descriptor_revision=descriptor.revision,
            status="idle",
            resources=[
                ResourceState(
                    resource_id=resource.id,
                    available=True,
                    properties={
                        name: TelemetryValue(value=20 if name == "temperature" else 0, unit=prop.unit)
                        for name, prop in resource.properties.items()
                    },
                )
                for resource in descriptor.resources
            ],
        )

    def publish(self):
        self.state.sequence += 1
        self.state.observed_at = utc_now()
        callback = self.callbacks.get("on_event")
        if callback:
            callback(
                DeviceEvent(
                    device_id=self.descriptor.device_id,
                    sequence=self.state.sequence,
                    event="device.snapshot",
                    data=self.state.model_dump(mode="json"),
                )
            )
        elif callback := self.callbacks.get("on_state_change"):
            callback(self.state)

    def emit_job(self, event):
        callback = self.callbacks.get("on_event")
        if callback and self.state.job is not None:
            self.state.sequence += 1
            callback(
                DeviceEvent(
                    device_id=self.descriptor.device_id,
                    sequence=self.state.sequence,
                    event=event,
                    data=JobEventData(
                        job=self.state.job, artifact=getattr(self, "job_artifact", None), snapshot=self.state
                    ).model_dump(mode="json"),
                )
            )

    def connect(self, loop=None):
        self.state.connected = True
        self.publish()

    def disconnect(self, timeout=0):
        self.state.connected = False

    def check_staleness(self):
        return self.state.connected


class ReferencePlugin(PrinterPlugin):
    id = "sdk_reference"
    name = "SDK development simulator"
    version = "1.0.0"
    api_version = 2
    publisher_id = "bambuddy.sdk.examples"
    models = (PrinterModel("simulator", "Resource API simulator"),)
    configuration_schema = {
        "type": "object",
        "properties": {
            "tool_count": {"type": "integer", "minimum": 1, "maximum": 64, "default": 6, "title": "Simulated tools"},
        },
        "additionalProperties": False,
    }

    def describe_device(self, config):
        count = int(config.options.get("tool_count", 6))
        resources = []
        for index in range(count):
            resources.extend(
                [
                    ResourceDescriptor(
                        id=f"tool:{index}",
                        kind="tool",
                        label=f"Tool {index}",
                        relations={"hotends": [f"hotend:{index}"], "extruders": [f"extruder:{index}"]},
                    ),
                    ResourceDescriptor(id=f"hotend:{index}", kind="hotend", label=f"Hotend {index}"),
                    ResourceDescriptor(id=f"extruder:{index}", kind="extruder", label=f"Extruder {index}"),
                    ResourceDescriptor(
                        id=f"dock:{index}", kind="dock", label=f"Dock {index}", relations={"accepts": [f"tool:{index}"]}
                    ),
                    ResourceDescriptor(
                        id=f"heater:{index}",
                        kind="heater",
                        label=f"Tool heater {index}",
                        relations={"heats": [f"hotend:{index}"]},
                        properties={
                            "temperature": ResourceProperty(
                                label="Temperature",
                                value_schema={"type": "number", "minimum": 0, "maximum": 500},
                                unit="degC",
                            ),
                            "target": ResourceProperty(
                                label="Target",
                                value_schema={"type": "number", "minimum": 0, "maximum": 400 + index * 20},
                                unit="degC",
                                writable=True,
                                operation_id="heater.set_target",
                                parameter="target",
                            ),
                        },
                    ),
                ]
            )
        return DeviceDescriptor(
            device_id=config.serial_number,
            model_id="simulator",
            name=self.name,
            resources=resources,
            configuration_schema=self.configuration_schema,
            operations=[
                OperationDescriptor(
                    id="heater.set_target",
                    label="Set heater target",
                    resource_kinds=["heater"],
                    parameters_schema={
                        "type": "object",
                        "properties": {"target": {"type": "number", "minimum": 0}},
                        "required": ["target"],
                        "additionalProperties": False,
                    },
                    result_schema={"type": "boolean"},
                ),
                OperationDescriptor(
                    id="job.start",
                    label="Start simulated job",
                    parameters_schema=JobRequest.model_json_schema(),
                    result_schema={"type": "boolean"},
                    permission="printers:files",
                ),
                *[
                    OperationDescriptor(
                        id=f"job.{name}", label=f"{name.title()} simulated job", result_schema={"type": "boolean"}
                    )
                    for name in ("stop", "pause", "resume")
                ],
                OperationDescriptor(
                    id="telemetry.refresh", label="Refresh simulator state", result_schema={"type": "boolean"}
                ),
            ],
            extensions={"bambuddy:development_only": True},
        )

    def create_connection(self, config, *, on_event=None, **callbacks):
        self.validate_config(config)
        if on_event is not None:
            callbacks["on_event"] = on_event
        return ReferenceConnection(self.describe_device(config), callbacks)

    def operation_handlers(self, connection):
        def heat(request):
            state = next(
                resource for resource in connection.state.resources if resource.resource_id == request.resource_id
            )
            state.properties["target"] = TelemetryValue(value=request.parameters["target"], unit="degC")
            connection.publish()
            return True

        def start(request):
            job = validate_job_request(connection.descriptor, request.parameters)
            connection.job_artifact = job.artifact
            connection.state.status = "printing"
            connection.state.job_id = job.job_id
            connection.state.job = JobProgress(
                job_id=job.job_id,
                state="printing",
                name=job.name,
                artifact_uri=job.artifact.uri,
                fraction=0,
                active_tool_ids=job.selected_tool_ids,
            )
            connection.publish()
            connection.emit_job("job.started")
            return True

        def phase(value):
            def change(request):
                connection.state.status = value
                if connection.state.job:
                    connection.state.job.state = value
                connection.publish()
                connection.emit_job("job.cancelled" if value == "cancelled" else "job.progress")
                return True

            return change

        def refresh(request):
            connection.publish()
            return True

        return {
            "heater.set_target": heat,
            "job.start": start,
            "job.stop": phase("cancelled"),
            "job.pause": phase("paused"),
            "job.resume": phase("printing"),
            "telemetry.refresh": refresh,
        }
