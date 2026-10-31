"""SDK job descriptions retain real input identities and validated resources."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.printers.contracts import PrinterConfig, PrinterModel, PrinterPlugin
from backend.app.printers.sdk import (
    SDK_API_VERSION,
    DeviceDescriptor,
    JobArtifact,
    JobDescription,
    JobInput,
    OperationDescriptor,
    PluginManifest,
    ResourceDescriptor,
    SDKValidationError,
    check_manifest_compatibility,
    validate_job_description,
)


def device():
    return DeviceDescriptor(
        device_id="test",
        resources=[
            ResourceDescriptor(id="tool:sixth", kind="tool", label="Sixth tool"),
            ResourceDescriptor(
                id="feed:opaque", kind="material_source", label="Source", relations={"tools": ["tool:sixth"]}
            ),
        ],
        operations=[
            OperationDescriptor(
                id="job.start",
                label="Start",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "options": {
                            "type": "object",
                            "properties": {"leveling": {"type": "boolean", "default": False}},
                            "additionalProperties": False,
                        }
                    },
                },
            )
        ],
    )


def test_job_description_validates_unique_input_and_resource_ids_without_vendor_positions():
    description = JobDescription(
        artifact=JobArtifact(uri="library:1", format="gcode"),
        inputs=[
            JobInput(
                id="original:input-7",
                label="Purple material",
                compatible_tool_ids=["tool:sixth"],
                compatible_source_ids=["feed:opaque"],
                routing_key="exact-material:purple",
            )
        ],
    )
    assert validate_job_description(device(), description).inputs[0].id == "original:input-7"
    for change in (
        {"compatible_tool_ids": ["feed:opaque"]},
        {"compatible_source_ids": ["tool:sixth"]},
        {"artifact_uri": "library:2"},
    ):
        invalid = description.model_copy(deep=True)
        invalid.inputs[0] = invalid.inputs[0].model_copy(update=change)
        with pytest.raises((SDKValidationError, ValueError)):
            validate_job_description(device(), invalid)
    with pytest.raises(ValidationError, match="unique"):
        JobDescription(artifact=description.artifact, inputs=[description.inputs[0], description.inputs[0]])


def test_older_provider_default_describes_declared_options_and_static_model_presentation():
    template = device().model_copy(update={"extensions": {"compatibility_ui": "fixture-v1"}})

    class Provider(PrinterPlugin):
        id = "fixture"
        name = "Fixture"
        models = (PrinterModel("fixture", "Fixture", device_template=template),)

        def create_connection(self, config, **callbacks):
            raise AssertionError("Job inspection must not create a printer connection")

        def describe_device(self, config):
            return template

    provider = Provider()
    config = PrinterConfig("", "fixture", "", plugin_id=provider.id)
    description = provider.describe_job(config, JobArtifact(uri="library:1", format="gcode"), Path("unused"))
    assert description.inputs == [] and description.default_options == {"leveling": False}
    assert provider.descriptor()["models"][0]["presentation"] == {"compatibility_ui": "fixture-v1"}


def test_job_description_hook_negotiates_sdk_minor_version_before_loading():
    manifest = PluginManifest(
        plugin_id="fixture",
        name="Fixture",
        version="1.0.0",
        publisher_id="fixture",
        entrypoint="plugin:Factory",
        requires_api=">=2.2.0,<3",
    )
    assert SDK_API_VERSION == "2.2.0"
    with pytest.raises(SDKValidationError, match="unsupported printer API"):
        check_manifest_compatibility(manifest, core_version="1.3.1b1", api_version="2.1.0")
    check_manifest_compatibility(manifest, core_version="1.3.1b1")
