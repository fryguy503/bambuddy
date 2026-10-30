"""Bridge core queue admission to normalized, resource-bound plugin job plans."""

from pathlib import PurePosixPath

from backend.app.printers.api import execute_host_command, printer_api
from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.registry import plugin_for
from backend.app.printers.sdk import (
    JobArtifact,
    JobRequest,
    MaterialBinding,
    OperationRequest,
    SDKValidationError,
    validate_job_request,
)


def queue_job_plan(item, config: PrinterConfig, filename: str) -> JobRequest:
    device = printer_api.describe_device(config)
    saved = getattr(item, "sdk_job", None)
    if saved is not None:
        job = JobRequest.model_validate(saved)
        job = job.model_copy(
            update={"job_id": f"queue:{item.id}", "artifact": job.artifact.model_copy(update={"uri": filename})}
        )
    else:
        tools = getattr(item, "tool_ids", None) or []
        available = [resource.id for resource in device.resources if resource.kind == "tool"]
        if not tools and len(available) == 1:
            tools = available
        if len(available) > 1 and not tools:
            raise SDKValidationError("Multi-tool jobs require an explicit resource tool plan")
        bindings = []
        for input_id, source_id in (getattr(item, "material_bindings", None) or {}).items():
            source = device.resource(source_id)
            eligible = set(source.relations.get("tools", [])) & set(tools)
            if len(eligible) == 1:
                tool_id = next(iter(eligible))
            elif len(tools) == 1:
                tool_id = tools[0]
            else:
                raise SDKValidationError("Ambiguous material routing requires an explicit sdk_job binding")
            bindings.append(MaterialBinding(input_id=input_id, tool_id=tool_id, source_id=source_id))
        job = JobRequest(
            job_id=f"queue:{item.id}",
            artifact=JobArtifact(
                uri=filename,
                format=PurePosixPath(filename).suffix.lstrip(".") or "unknown",
                filename=PurePosixPath(filename).name,
            ),
            selected_tool_ids=tools,
            bindings=bindings,
            options=getattr(item, "job_options", None) or {},
            plate_id=str(item.plate_id) if getattr(item, "plate_id", None) is not None else None,
        )
    return validate_job_request(device, job)


async def start_queue_job(item, printer, connection, filename: str, **legacy_options) -> bool:
    plugin = plugin_for(printer)
    # The installed Bambu compatibility adapter keeps its existing dispatch
    # behavior until a caller supplies a normalized SDK plan. New providers
    # implement job.start and never need the legacy vendor option vocabulary.
    explicit = any(getattr(item, name, None) for name in ("sdk_job", "tool_ids", "material_bindings", "job_options"))
    if plugin.api_version == 2 and (explicit or "start_print" not in plugin.operations):
        config = PrinterConfig.from_printer(printer)
        job = queue_job_plan(item, config, filename)
        receipt = await printer_api.invoke(
            config,
            connection._connection if hasattr(connection, "_connection") else connection,
            OperationRequest(operation_id="job.start", parameters=job.model_dump(mode="json", exclude_none=True)),
        )
        return receipt.status in {"accepted", "running", "succeeded"} and receipt.result is not False
    return bool(await execute_host_command(connection, "start_print", filename, **legacy_options))
