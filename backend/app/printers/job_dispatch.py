"""Bridge core queue admission to normalized, resource-bound plugin job plans."""

import inspect
from copy import deepcopy
from pathlib import Path, PurePosixPath

from backend.app.printers.api import execute_host_command, printer_api
from backend.app.printers.contracts import PrinterConfig, PrinterPlugin, PrinterPluginError
from backend.app.printers.registry import plugin_for
from backend.app.printers.sdk import (
    JobArtifact,
    JobPreflightResult,
    JobRequest,
    MaterialBinding,
    OperationRequest,
    SDKValidationError,
    validate_job_preflight,
    validate_job_request,
)

QUEUE_HOLD_KEY = "bambuddy:queue_hold"


def queue_job_revision(item) -> dict:
    """Detached inputs used to detect edits while a provider is checking them."""
    return deepcopy(
        {
            name: getattr(item, name, None)
            for name in (
                "printer_id",
                "archive_id",
                "library_file_id",
                "tool_ids",
                "material_bindings",
                "job_options",
                "sdk_job",
                "plate_id",
                "dispatching_at",
                "manual_start",
            )
        }
    )


def without_queue_hold(plan: dict) -> dict:
    extensions = dict(plan.get("extensions") or {})
    extensions.pop(QUEUE_HOLD_KEY, None)
    return {**plan, "extensions": extensions}


def queue_hold(result: JobPreflightResult) -> dict | None:
    if result.ready:
        return None
    first = next((issue for issue in result.issues if issue.recovery == "material_selection"), result.issues[0])
    return {
        "kind": first.recovery,
        "code": first.code,
        "message": " ".join("".join(char if char.isprintable() else " " for char in first.message).split())[:2000],
        "input_ids": sorted({identity for issue in result.issues for identity in issue.input_ids}),
        "resource_ids": sorted({identity for issue in result.issues for identity in issue.resource_ids}),
    }


class JobDispatchRejected(PrinterPluginError):
    """A validated public receipt rejection, without private error details."""

    def __init__(self, receipt):
        error = receipt.error
        self.code = "".join(char for char in error.code if char.isprintable())[:128] if error else "job.start_rejected"
        message = error.message if error else "The printer support rejected the print command."
        self.preflight = None
        if error and error.code == "job.material_selection_required" and error.details.get("not_submitted") is True:
            try:
                self.preflight = JobPreflightResult.model_validate({"issues": error.details.get("issues")})
            except ValueError as exc:
                raise SDKValidationError("Invalid material deferral result") from exc
            if self.preflight.ready:
                raise SDKValidationError("A material deferral requires a preflight issue")
        super().__init__("".join(char if char.isprintable() else " " for char in message).strip()[:2000])


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


async def preflight_queue_job(item, printer, connection, path: Path) -> JobPreflightResult:
    """Reuse the inspection worker for optional, read-only provider readiness."""
    from backend.app.printers.job_inspection import _in_worker

    config = PrinterConfig.from_printer(printer)
    plugin = plugin_for(config)

    callback = plugin.preflight_job
    if getattr(callback, "__func__", None) is PrinterPlugin.preflight_job:
        return JobPreflightResult()
    job = queue_job_plan(item, config, path.name)
    job = job.model_copy(update={"extensions": without_queue_hold(job.model_dump(mode="json"))["extensions"]})
    raw = getattr(connection, "_connection", connection)
    if inspect.iscoroutinefunction(callback):
        result = await callback(config, raw, job, path)
    else:
        result = await _in_worker(lambda: callback(config, raw, job, path))
        if inspect.isawaitable(result):
            result = await result
    return await _in_worker(lambda: validate_job_preflight(plugin.describe_device(config), job, result))


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
            connection,
            OperationRequest(operation_id="job.start", parameters=job.model_dump(mode="json", exclude_none=True)),
        )
        if receipt.status not in {"accepted", "running", "succeeded"} or receipt.result is False:
            rejected = JobDispatchRejected(receipt)
            if rejected.preflight is not None:
                validate_job_preflight(printer_api.describe_device(config), job, rejected.preflight)
            raise rejected
        return True
    return bool(await execute_host_command(connection, "start_print", filename, **legacy_options))
