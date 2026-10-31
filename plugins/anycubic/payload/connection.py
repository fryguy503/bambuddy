"""Typed connection facade, receipt tracking and durable no-replay dispatch."""

import asyncio
import hashlib
import logging
import re
import threading
import time
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Literal

from backend.app.printers.sdk import (
    DeviceEvent,
    DeviceSnapshot,
    JobArtifact,
    JobProgress,
    JobRequest,
    OperationError,
    OperationReceipt,
    validate_invocation,
    validate_job_request,
)

from .client import AnycubicMQTTClient
from .device import device_id, native_slot, snapshot
from .printing import inspect_print_file, print_data, validate_options
from .protocol import AnycubicError, task_id as native_task_id

logger = logging.getLogger(__name__)


class AnycubicConnection:
    def __init__(self, config, plugin, *, on_event=None, **callbacks):
        from .persistence import Journal

        self.config = config
        self.plugin = plugin
        self.printer_key = hashlib.sha256(config.serial_number.encode()).hexdigest()[:24]
        self.journal = Journal(self.printer_key)
        hold = self.journal.get("plate:" + self.printer_key)
        self.plate_clear_required = bool(hold and hold.get("status") == "plate_required")
        self.native = AnycubicMQTTClient(
            config.ip_address,
            config.serial_number,
            model=config.model,
            tls_fingerprint=config.options["tls_fingerprint"],
            on_state_change=self._changed,
        )
        self.on_event = on_event
        self.callbacks = callbacks
        self.loop = None
        self.sequence = 0
        self.lock = threading.RLock()
        self.tasks = {}
        self.started = set()
        self.terminal = set()
        self.current_job = None
        self.capture_lock = asyncio.Lock()
        self.inventory_links = {}
        self.inventory_pending = False
        self.unit_names = {}
        for box in self.native._model.ace_ids:
            resource = f"ace:{box}"
            saved_name = self.journal.get("name:" + self.printer_key + ":" + resource)
            if saved_name:
                self.unit_names[resource] = saved_name.get("name", "")
        self.native._display_names = self.unit_names

    @property
    def state(self) -> DeviceSnapshot:
        with self.native._lock:
            return snapshot(
                self.plugin,
                self.config,
                self.native,
                self.sequence,
                self.current_job,
                self.inventory_links,
                self.unit_names,
                plate_clear_required=self.plate_clear_required,
            )

    @property
    def last_connect_error(self):
        return self.native.last_connect_error

    @property
    def report_messages_since_connect(self):
        return self.native.report_messages_since_connect

    @property
    def logging_enabled(self):
        return self.native.logging_enabled

    @property
    def _drying_targets(self):
        return {}

    def connect(self, loop=None):
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
        self.loop = loop
        self.inventory_pending = False

        async def restore_links():
            from . import inventory

            self.inventory_links = await inventory.snapshot(self.config)
            for record in self.journal.pending_accounting():
                try:
                    result = await inventory.complete(self.config, record, record["pending_outcome"])
                    if not isinstance(result, dict) or result.get("state") != "deferred":
                        self.journal.update(record["job_id"], pending_outcome=None)
                except Exception:
                    logger.warning("Anycubic deferred usage accounting remains pending", exc_info=True)

        self._schedule(restore_links())
        self.native.connect(loop)

    def disconnect(self, timeout: float = 0):
        self.native.disconnect(timeout)

    def check_staleness(self):
        return self.native.check_staleness()

    def _emit(self, event, data):
        self.sequence += 1
        if self.on_event is not None:
            self.on_event(DeviceEvent(device_id=device_id(self.config), sequence=self.sequence, event=event, data=data))
        elif event == "device.snapshot" and self.callbacks.get("on_state_change"):
            self.callbacks["on_state_change"](self.state)

    def _schedule(self, coroutine):
        if self.loop is not None and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        else:
            coroutine.close()

    def _plate(self, blocked):
        key = "plate:" + self.printer_key
        if self.journal.get(key) is None:
            self.journal.begin(
                key, {"printer_key": self.printer_key, "status": "plate_required" if blocked else "resolved"}
            )
        else:
            self.journal.update(key, status="plate_required" if blocked else "resolved")
        self.plate_clear_required = bool(blocked)
        if self.config.printer_id is not None:
            from backend.app.services.printer_manager import printer_manager

            printer_manager.set_awaiting_plate_clear(self.config.printer_id, blocked)

    def _changed(self, ignored=None):
        with self.lock:
            try:
                self._observe_jobs()
                self._emit("device.snapshot", self.state.model_dump(mode="json"))
                boxes = self.native.ace_material_snapshot()
                if boxes is not None:
                    from . import inventory

                    if not self.inventory_pending:
                        self.inventory_pending = True

                        async def refresh_inventory():
                            try:
                                await inventory.observe(self.config, boxes)
                                self.inventory_links = await inventory.snapshot(self.config)
                                from .history import record

                                await asyncio.to_thread(record, self.config, boxes)
                            finally:
                                self.inventory_pending = False

                        self._schedule(refresh_inventory())
            except Exception:
                logger.exception("Anycubic typed status projection failed")

    def _observe_jobs(self):
        state = self.native.state
        if not state.connected or self.native.is_stale or not self.native._telemetry.lifecycle_observed:
            return
        filename = PurePosixPath(state.gcode_file or "").name
        task = native_task_id(state.subtask_id)
        for record in self.journal.unresolved(self.printer_key):
            job_id = record["job_id"]
            if filename != record.get("native_filename"):
                continue
            expected = native_task_id(record.get("native_job_id"))
            if expected is None or task != expected:
                # A filename alone cannot attribute a printer-started job to
                # this durable dispatch. The native receipt must establish its
                # real identity first; a later report can then acquire it.
                continue
            active = state.state in {"RUNNING", "PAUSE", "PREPARE"}
            terminal = {"FINISH": "completed", "FAILED": "failed"}.get(state.state)
            if state.state == "FAILED" and self.native._telemetry.phase in {"stoped", "stopped"}:
                terminal = "cancelled"
            if not active and not (terminal and record.get("status") == "running"):
                continue
            job = JobProgress(
                job_id=job_id,
                state={"RUNNING": "printing", "PAUSE": "paused", "PREPARE": "preparing"}.get(
                    state.state, terminal or "unknown"
                ),
                name=record.get("uri"),
                artifact_uri=record.get("uri"),
                fraction=max(0, min(1, state.progress / 100)),
                remaining_seconds=max(0, state.remaining_time) * 60,
                layer=max(0, state.layer_num),
                total_layers=max(0, state.total_layers),
                active_tool_ids=["tool:primary"],
                extensions={"native_job_id": task, "native_filename": filename},
            )
            self.current_job = job
            artifact = JobArtifact(
                uri=record["uri"],
                filename=record["uri"],
                format="gcode",
                sha256=record.get("sha256"),
                metadata=record.get("artifact", {}).get("estimates", {}),
            )
            if active:
                self.journal.update(job_id, status="running", native_job_id=task or expected)
                if job_id not in self.started:
                    self.started.add(job_id)
                    self._emit(
                        "job.started",
                        {"job": job.model_dump(mode="json"), "artifact": artifact.model_dump(mode="json")},
                    )
                else:
                    self._emit("job.progress", {"job": job.model_dump(mode="json")})
            elif terminal is not None and job_id not in self.terminal:
                self.terminal.add(job_id)
                # Keep the durable intent pending until the host commits its
                # archive callback. A restart can then replay this observation,
                # never the original hardware command.
                self.journal.update(job_id, pending_outcome=terminal)
                self._plate(True)
                self._emit(
                    "job." + terminal,
                    {"job": job.model_dump(mode="json"), "artifact": artifact.model_dump(mode="json")},
                )

    def can_deactivate(self):
        return not (
            self.native._print_pending
            or self.native.state.connected
            and self.native.state.state == "BUSY"
            or self.native._manual_feed
            or self.native._extrusion_pending
            or any(box.get("anycubic_drying", {}).get("status") == 1 for box in self.native._telemetry.boxes.values())
            or self.journal.unresolved(self.printer_key)
            or any(
                task["receipt"].status in {"accepted", "running"} and time.monotonic() - task["started"] < 60
                for task in self.tasks.values()
            )
        )

    def _receipt(
        self,
        request,
        status: Literal["accepted", "running", "succeeded", "failed", "cancelled"] = "succeeded",
        result=None,
        *,
        error=None,
        task_id=None,
    ) -> OperationReceipt:
        return OperationReceipt(
            operation_id=request.operation_id,
            request_id=request.request_id,
            status=status,
            result=result,
            error=error,
            task_id=task_id,
        )

    async def _start_job(self, request):
        from . import inventory

        descriptor = self.plugin.describe_device(self.config)
        job = validate_job_request(descriptor, JobRequest.model_validate(request.parameters))
        if set(job.selected_tool_ids) != {"tool:primary"}:
            raise AnycubicError("Select this printer's primary physical tool")
        hold = self.journal.get("plate:" + self.printer_key)
        if hold and hold.get("status") == "plate_required":
            raise AnycubicError("Confirm plate cleared through the Anycubic plate operation before another job")
        previous = self.journal.get(job.job_id)
        if previous is not None:
            if previous.get("bindings") != job.model_dump(mode="json"):
                raise AnycubicError("This dispatch identity already belongs to a different job plan")
            if previous.get("sha256") != job.artifact.sha256 and job.artifact.sha256 is not None:
                raise AnycubicError("This job identity already belongs to a different artifact")
            if previous.get("status") in {"accepted", "running"}:
                return self._receipt(
                    request,
                    previous["status"],
                    {"delivery": "native_receipt", "hardware_confirmed": previous["status"] == "running"},
                    task_id=job.job_id,
                )
            raise AnycubicError("This dispatch was already attempted; resolve its outcome before creating a new job")
        if self.journal.unresolved(self.printer_key):
            raise AnycubicError("Resolve the previous native dispatch before starting another")
        saved = self.journal.artifact(self.printer_key, job.artifact.uri)
        if saved is None:
            raise AnycubicError("Upload and confirm this sliced artifact before starting it")
        artifact = await asyncio.to_thread(
            inspect_print_file, Path(saved["path"]), saved["source_filename"], self.config.model
        )
        artifact = replace(artifact, file=replace(artifact.file, filename=saved["native_filename"]))
        if (
            artifact.file.sha256 != saved["sha256"]
            or job.artifact.sha256 is not None
            and job.artifact.sha256 != saved["sha256"]
        ):
            raise AnycubicError("The retained artifact changed since upload")
        mapping = []
        inputs = {}
        required = {filament.tool for filament in artifact.filaments}
        numeric = all(binding.input_id.isdecimal() for binding in job.bindings)
        numeric_ids = {int(binding.input_id) for binding in job.bindings} if numeric else set()
        offset = 1 if numeric_ids != required and numeric_ids == {tool + 1 for tool in required} else 0
        for binding in job.bindings:
            match = re.fullmatch(r"(?:T|input:)?(\d+)", binding.input_id)
            if not match:
                raise AnycubicError("Material input IDs must identify original sliced T inputs")
            tool = int(match[1]) - offset
            if tool in inputs.values():
                raise AnycubicError("Each sliced input requires exactly one material binding")
            inputs[binding.input_id] = tool
            if binding.source_id == "material:external":
                continue
            box, slot = native_slot(binding.source_id or binding.slot_id or "")
            mapping.append({"tool": tool, "box_id": box, "slot_id": slot})
        if job.bindings and set(inputs.values()) != required:
            raise AnycubicError("Bind every original sliced material input exactly once")
        options = validate_options(
            {
                "use_ace": bool(mapping),
                "filament_mapping": mapping,
                **{
                    name: value
                    for name, value in job.options.items()
                    if name
                    in {
                        "auto_leveling",
                        "vibration_compensation",
                        "flow_calibration",
                        "timelapse",
                        "allow_material_type_mismatch",
                    }
                },
            }
        )
        if set(job.options) - {
            "auto_leveling",
            "vibration_compensation",
            "flow_calibration",
            "timelapse",
            "use_ace",
            "allow_material_type_mismatch",
        }:
            raise AnycubicError("Unsupported native print option")
        if "use_ace" in job.options and job.options["use_ace"] != bool(mapping):
            raise AnycubicError("ACE selection must agree with the explicit material bindings")
        # Reject known invalid material choices before reserving a durable intent.
        # submit_print repeats this check against fresh capabilities after its
        # final file/hash preflight, so this does not grant dispatch authority.
        capabilities = self.native.print_capabilities()
        print_data(artifact, options, capabilities["ace_slots"], capabilities["options"])
        usage_job = job.model_copy(
            update={
                "bindings": [
                    binding.model_copy(update={"input_id": "T" + str(inputs[binding.input_id])})
                    for binding in job.bindings
                ]
            }
        )
        usage = await inventory.capture(self.config, usage_job, {"estimates": artifact.estimates})
        record = {
            "job_id": job.job_id,
            "printer_key": self.printer_key,
            "uri": job.artifact.uri,
            "native_filename": artifact.file.filename,
            "sha256": artifact.file.sha256,
            "status": "intent",
            "native_job_id": None,
            "artifact": saved,
            "options": options,
            "bindings": job.model_dump(mode="json"),
            "usage_snapshot": usage,
            "request_id": request.request_id,
        }
        if not self.journal.begin(job.job_id, record):
            raise AnycubicError("This dispatch identity was already recorded")
        try:
            context = await asyncio.to_thread(self.native.file_session, require_upload=False)
            result = await asyncio.to_thread(
                self.native.submit_print, context, artifact, options, hashlib.sha256(job.job_id.encode()).hexdigest()
            )
            status = result.get("status")
            current = self.journal.get(job.job_id)
            observed_status = current.get("status") if current else None
            self.journal.update(
                job.job_id,
                status=observed_status
                if observed_status in {"running", "completed", "failed", "cancelled"}
                else "accepted"
                if status == "accepted"
                else "unknown"
                if status != "rejected"
                else "failed",
                native_job_id=native_task_id(result.get("task_id")),
            )
            if status == "accepted":
                return self._receipt(
                    request, "accepted", {"delivery": "native_receipt", "hardware_confirmed": False}, task_id=job.job_id
                )
            raise AnycubicError(
                "Native dispatch outcome is uncertain; inspect the printer and resolve the hold without replaying it"
                if status != "rejected"
                else "The printer rejected this print"
            )
        except BaseException:
            saved_record = self.journal.get(job.job_id)
            if saved_record and saved_record.get("status") == "intent":
                self.journal.update(job.job_id, status="unknown")
            raise

    async def invoke(self, request):
        from . import inventory

        operation = validate_invocation(self.plugin.describe_device(self.config), request)
        op = request.operation_id
        p = request.parameters
        resource = request.resource_id
        try:
            if op == "job.start":
                return await self._start_job(request)
            if op == "anycubic.discovery.inspect":
                return self._receipt(
                    request, result=await asyncio.to_thread(AnycubicMQTTClient.discover, p["ip_address"])
                )
            if op == "anycubic.files.list":
                return self._receipt(request, result=await asyncio.to_thread(self.native.list_files, **p))
            if op == "anycubic.ace.history":
                from .history import readings

                return self._receipt(
                    request,
                    result=await asyncio.to_thread(
                        readings, self.config, int(resource.split(":")[-1]), p.get("hours", 24)
                    ),
                )
            if op == "anycubic.inventory.assign":
                self.inventory_links = await inventory.assign(self.config, resource, p["spool_id"])
                return self._receipt(request, result=self.inventory_links)
            if op == "anycubic.ace.rename":
                key = "name:" + self.printer_key + ":" + resource
                if self.journal.get(key) is None:
                    self.journal.begin(key, {"status": "metadata", "name": p["name"]})
                else:
                    self.journal.update(key, name=p["name"])
                self.unit_names[resource] = p["name"]
                return self._receipt(request, result=True)
            if op in {"job.clear_plate", "anycubic.plate.clear", "anycubic.job.resolve"}:
                if self.native.state.state != "IDLE" or not self.native._motion_ready():
                    raise AnycubicError("A fresh idle report is required")
                if op == "anycubic.job.resolve":
                    record = self.journal.get(p["job_id"])
                    if (
                        not record
                        or record.get("printer_key") != self.printer_key
                        or record.get("status") not in {"intent", "unknown", "accepted"}
                    ):
                        raise AnycubicError("Select an unresolved dispatch belonging to this printer")
                    self.journal.update(p["job_id"], status="resolved")
                    self._plate(True)
                else:
                    if self.journal.unresolved(self.printer_key):
                        raise AnycubicError("Resolve the held job before confirming the plate")
                    self._plate(False)
                self._emit("device.snapshot", self.state.model_dump(mode="json"))
                return self._receipt(request, result=True)
            calls = {
                "heater.set_target": lambda: (
                    self.native.set_nozzle_temperature(p["target"])
                    if resource == "heater:nozzle"
                    else self.native.set_bed_temperature(p["target"])
                ),
                "fan.set_percent": lambda: self.native.set_fan_percent(resource.split(":")[-1], p["percent"]),
                "light.set": lambda: self.native.set_chamber_light(p["on"]),
                "job.pause": self.native.pause_print,
                "job.resume": self.native.resume_print,
                "job.stop": self.native.stop_print,
                "telemetry.refresh": self.native.request_status_update,
                "anycubic.settings.speed": lambda: self.native.set_print_speed(p["mode"]),
                "anycubic.motion.jog": lambda: self.native.jog_axis(resource.split(":")[-1], p["distance"]),
                "anycubic.motion.home": lambda: self.native.home_axes(p["axes"]),
                "anycubic.extruder.pulse": lambda: self.native.extruder_pulse(
                    {"extrude": 1, "retract": 0}[p["direction"]]
                ),
                "anycubic.extruder.stop": self.native.stop_extrusion,
                "anycubic.material.configure": lambda: self.native.set_ace_slot(
                    *native_slot(resource), p["material"], p["color"].removeprefix("#")[:6]
                ),
                "anycubic.material.feed": lambda: self.native.feed_ace_slot(*native_slot(resource), p["action"]),
                "anycubic.material.refresh": self.native.request_status_update,
                "anycubic.drying.set": lambda: self.native.set_ace_drying(
                    int(resource.split(":")[-1]), temperature=p["temperature"], duration_minutes=p["duration_minutes"]
                ),
                "anycubic.drying.stop": lambda: self.native.set_ace_drying(int(resource.split(":")[-1]), stop=True),
            }
            result = await asyncio.to_thread(calls[op])
            if not result:
                raise AnycubicError("The command requires fresh supported state and an idle printer where applicable")
            if not operation.asynchronous:
                return self._receipt(request, result=True)
            # Keep the request identity recoverable after a process restart.
            # Hardware commands are never replayed to recreate a lost observer.
            task = "c|" + request.operation_id + "|" + request.request_id
            receipt = self._receipt(
                request, "accepted", {"delivery": "queued", "hardware_confirmed": False}, task_id=task
            )
            self.tasks[task] = {
                "receipt": receipt,
                "request": request,
                "started": time.monotonic(),
                "baseline": self.native._last_message_monotonic,
            }
            return receipt
        except (AnycubicError, ValueError, KeyError, OSError) as exc:
            return self._receipt(
                request,
                "failed",
                error=OperationError(
                    code="anycubic.operation_rejected",
                    message=str(exc)
                    if isinstance(exc, AnycubicError)
                    else "The native operation could not be completed",
                    retryable=False,
                ),
            )

    async def operation_status(self, task_id):
        record = self.journal.get(task_id)
        if record is not None and record.get("job_id"):
            status = record.get("status")
            from backend.app.printers.sdk import OperationRequest

            request = OperationRequest(operation_id="job.start", request_id=record.get("request_id", task_id))
            if status in {"accepted", "running"}:
                return self._receipt(request, status, task_id=task_id)
            return self._receipt(
                request,
                "succeeded" if status == "completed" else "failed",
                result=True if status == "completed" else None,
                error=None
                if status == "completed"
                else OperationError(
                    code="anycubic.dispatch_outcome", message="The native job ended without confirmed completion"
                ),
            )
        task = self.tasks.get(task_id)
        if task is None:
            parts = task_id.split("|", 2)
            if len(parts) == 3 and parts[0] == "c":
                from backend.app.printers.sdk import OperationRequest

                self.plugin.describe_device(self.config).operation(parts[1])
                request = OperationRequest(operation_id=parts[1], request_id=parts[2])
                return self._receipt(
                    request,
                    "failed",
                    error=OperationError(
                        code="anycubic.observer_restarted",
                        message="The command observer restarted; inspect current telemetry before another command",
                        retryable=False,
                    ),
                )
            raise AnycubicError("Unknown native operation task")
        receipt = task["receipt"]
        if receipt.status not in {"accepted", "running"}:
            return receipt
        request = task["request"]
        op = request.operation_id
        resource = request.resource_id
        p = request.parameters
        fresh = self.native._last_message_monotonic > task["baseline"] and not self.native.is_stale
        observed = False
        if fresh:
            if op == "heater.set_target":
                observed = (
                    self.native.state.temperatures.get(("nozzle" if resource == "heater:nozzle" else "bed") + "_target")
                    == p["target"]
                )
            elif op == "light.set":
                observed = self.native.state.chamber_light == p["on"]
            elif op == "anycubic.settings.speed":
                observed = self.native.state.speed_level == p["mode"]
            elif op in {"job.pause", "job.resume", "job.stop"}:
                observed = (
                    self.native.state.state
                    in {"job.pause": {"PAUSE"}, "job.resume": {"RUNNING"}, "job.stop": {"FAILED", "FINISH", "IDLE"}}[op]
                )
            elif op == "anycubic.material.refresh":
                observed = True
            elif op == "fan.set_percent":
                field = {"part": "cooling_fan_speed", "aux": "big_fan1_speed", "chamber": "big_fan2_speed"}[
                    resource.split(":")[-1]
                ]
                observed = getattr(self.native.state, field, None) == p["percent"]
            elif op in {"anycubic.drying.set", "anycubic.drying.stop"}:
                dry = self.native._telemetry.boxes.get(int(resource.split(":")[-1]), {}).get("anycubic_drying", {})
                observed = (
                    dry.get("status") == 0
                    if op.endswith("stop")
                    else (
                        dry.get("status") == 1
                        and dry.get("target_temp") == p["temperature"]
                        and dry.get("duration_minutes") == p["duration_minutes"]
                    )
                )
            elif op == "anycubic.material.configure":
                box, slot = native_slot(resource)
                tray = next(
                    (
                        item
                        for item in self.native._telemetry.boxes.get(box, {}).get("tray", [])
                        if item.get("id") == slot
                    ),
                    {},
                )
                observed = (
                    tray.get("tray_type", "").casefold() == p["material"].strip().casefold()
                    and tray.get("tray_color", "")[:6].casefold() == p["color"].removeprefix("#")[:6].casefold()
                )
            elif op in {"anycubic.extruder.pulse", "anycubic.extruder.stop"}:
                observed = (
                    self.native._extrude_observed > task["baseline"]
                    and self.native._extrude_operate == 0
                    and not self.native._extrusion_pending
                )
        if observed:
            receipt = self._receipt(request, result={"delivery": "observed", "hardware_confirmed": True})
        elif time.monotonic() - task["started"] >= 60:
            receipt = self._receipt(
                request,
                "failed",
                error=OperationError(
                    code="anycubic.observation_timeout",
                    message="The queued command was not confirmed by matching native telemetry",
                    retryable=False,
                ),
            )
        task["receipt"] = receipt
        return receipt

    async def cancel_operation(self, task_id):
        raise AnycubicError("Queued native commands cannot be unsent; use an explicit supported stop operation")
